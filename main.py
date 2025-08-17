# publisher.py
import os, json, time, random
import numpy as np
import paho.mqtt.client as mqtt

# =========================
# Konfigurasi
# =========================
MQTT_URL = os.getenv("MQTT_URL", "mqtt://localhost")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_FRESH = os.getenv("TOPIC_FRESH", "fish/fuzzy/freshness")
TOPIC_PRESV = os.getenv("TOPIC_PRESV", "fish/fuzzy/preservation")

# Hardcode waktu baca sensor (ms) -> SESUAIKAN dengan datasheet sensor Anda
SENSOR_READ_MS = {
    "ph": 25,          # misal modul pH analog
    "gas": 100,        # misal MQ-135 sampling
    "temp": 200,       # misal DS18B20 resolusi tinggi
    "hum": 200,        # misal SHT/DHT read window (contoh)
}

# =========================
# Utilitas Fuzzy
# =========================
def trimf(x, a, b, c):
    # sama seperti di notebook: segitiga sederhana
    return np.maximum(np.minimum((x - a) / (b - a + 1e-6), (c - x) / (c - b + 1e-6)), 0)

# Domain sesuai lampiran
g_dom = np.linspace(0, 200, 501)   # Gas
p_dom = np.linspace(0, 14, 281)    # pH
t_dom = np.linspace(0, 25, 251)    # Suhu (°C)
h_dom = np.linspace(50, 100, 251)  # Kelembaban (%)
k_dom = np.linspace(0, 100, 1001)  # Output 0..100

# MF GAS (dari notebook)
def gas_mf(val):
    rendah = trimf(val, 0, 0, 75)
    sedang = trimf(val, 50, 100, 150)
    tinggi = trimf(val, 100, 200, 200)
    return {"rendah": rendah, "sedang": sedang, "tinggi": tinggi}

# MF pH (dari notebook)
def ph_mf(val):
    asam   = trimf(val, 0, 0, 6.0)
    netral = trimf(val, 6.0, 7.0, 8.0)
    basa   = trimf(val, 7.0, 14.0, 14.0)
    return {"asam": asam, "netral": netral, "basa": basa}

# MF SUHU (dari notebook)
def suhu_mf(val):
    dingin = trimf(val, 0, 0, 12.5)
    hangat = trimf(val, 8, 12.5, 17)
    panas  = trimf(val, 12.5, 25, 25)
    return {"dingin": dingin, "hangat": hangat, "panas": panas}

# MF KELEMBABAN (dari notebook)
def hum_mf(val):
    rendah = trimf(val, 50, 50, 75)
    sedang = trimf(val, 60, 75, 90)
    tinggi = trimf(val, 75, 100, 100)
    return {"rendah": rendah, "sedang": sedang, "tinggi": tinggi}

# MF Output (Kesegaran 0..100) – sesuai notebook
def output_base_curves(k_values):
    return {
        "B":  trimf(k_values, 0,   0,   25),
        "KS": trimf(k_values, 20,  37.5, 55),
        "S":  trimf(k_values, 45,  62.5, 80),
        "SS": trimf(k_values, 70, 100, 100),
    }

def centroid_defuzz(x, mu):
    area = np.trapz(mu, x)
    if area <= 1e-9:
        return 0.0
    return float(np.trapz(x * mu, x) / area)

def classify_from_centroid(c, curves):
    # klasifikasi crisp berdasar membership tertinggi di titik centroid
    vals = {k: float(np.interp(c, k_dom, v)) for k, v in curves.items()}
    return max(vals.items(), key=lambda kv: kv[1])[0], vals

def mamdani_aggregate(antecedents, rules, base_curves):
    """
    antecedents: dict seperti {"gas":{"rendah":μ,...}, "ph":{"asam":μ,...}}
                 atau {"temp":{...}, "hum":{...}}
    rules: [ (('rendah','netral'), 'SS'), ...]  with None as don't-care
    base_curves: {'B': curve, 'KS': curve, 'S': curve, 'SS': curve} on k_dom
    """
    agg = {k: np.zeros_like(k_dom) for k in base_curves.keys()}
    for (a1, a2), out_cat in rules:
        mu1 = antecedents[0].get(a1, 1.0) if a1 is not None else 1.0
        mu2 = antecedents[1].get(a2, 1.0) if a2 is not None else 1.0
        alpha = min(mu1, mu2)
        clipped = np.minimum(base_curves[out_cat], alpha)
        agg[out_cat] = np.maximum(agg[out_cat], clipped)
    # agregasi keseluruhan (max) untuk defuzz
    mu_agg = np.zeros_like(k_dom)
    for v in agg.values():
        mu_agg = np.maximum(mu_agg, v)
    return mu_agg, agg

# =========================
# RULES (dari notebook untuk KESegaran: gas × pH)
# =========================
RULES_FRESH = [
    (('rendah','netral'), 'SS'),
    (('rendah','asam'),    'S'),
    (('rendah','basa'),    'KS'),
    (('sedang','netral'),  'S'),
    (('sedang','asam'),    'KS'),
    (('sedang','basa'),    'B'),
    (('tinggi',None),      'B'),
    ((None,'basa'),        'B'),
    (('tinggi','netral'),  'B'),
]

# =========================
# RULES (usulan wajar untuk PENGAWETAN: suhu × kelembaban)
# Catatan: lampiran tidak memuat aturan eksplisit untuk ini.
# Aturan berikut umum/logis untuk preservasi (silakan sesuaikan bila ada aturan resmi):
# =========================
RULES_PRESV = [
    (('dingin','rendah'), 'SS'),
    (('dingin','sedang'), 'S'),
    (('dingin','tinggi'), 'KS'),
    (('hangat','rendah'), 'S'),
    (('hangat','sedang'), 'KS'),
    (('hangat','tinggi'), 'B'),
    (('panas','rendah'),  'KS'),
    (('panas','sedang'),  'B'),
    (('panas','tinggi'),  'B'),
]

# =========================
# Komputasi per sistem
# =========================
def compute_freshness(ph_val, gas_val):
    t0 = time.time()
    # Fuzzifikasi
    g_m = gas_mf(gas_val)
    p_m = ph_mf(ph_val)
    # Inferensi + agregasi
    bases = output_base_curves(k_dom)
    mu_agg, detail = mamdani_aggregate((g_m, p_m), RULES_FRESH, bases)
    # Defuzzifikasi
    c = centroid_defuzz(k_dom, mu_agg)
    cat, peaks = classify_from_centroid(c, bases)
    t1 = time.time()
    return {
        "centroid": c,
        "category": cat,
        "peaks_at_centroid": peaks,
        "algorithm_ms": (t1 - t0) * 1000.0
    }

def compute_preservation(temp_val, hum_val):
    t0 = time.time()
    # Fuzzifikasi
    t_m = suhu_mf(temp_val)
    h_m = hum_mf(hum_val)
    # Inferensi + agregasi
    bases = output_base_curves(k_dom)
    mu_agg, detail = mamdani_aggregate((t_m, h_m), RULES_PRESV, bases)
    # Defuzzifikasi
    c = centroid_defuzz(k_dom, mu_agg)
    cat, peaks = classify_from_centroid(c, bases)
    t1 = time.time()
    return {
        "centroid": c,
        "category": cat,
        "peaks_at_centroid": peaks,
        "algorithm_ms": (t1 - t0) * 1000.0
    }

# =========================
# MQTT Publisher
# =========================
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fuzzy-python-publisher")

def publish(topic, payload):
    client.publish(topic, json.dumps(payload), qos=1, retain=False)

def main():
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    # Contoh data uji (Anda bisa ganti membaca dari sensor asli)
    samples = [
        # (pH, gas_ppm, temp_C, hum_%)
        (7.0, 60, 8.0, 65),
        (6.5, 140, 14.0, 85),
        (8.2, 30,  4.0, 55),
        (7.4, 110, 23.0, 92),
    ]

    for ph_val, gas_val, temp_val, hum_val in samples:
        # ----- Kesegaran -----
        sensor_ms_fresh = SENSOR_READ_MS["ph"] + SENSOR_READ_MS["gas"]
        res_fresh = compute_freshness(ph_val, gas_val)
        payload_fresh = {
            "type": "freshness",
            "inputs": {"ph": ph_val, "gas": gas_val},
            "sensor_read_ms": {
                "ph": SENSOR_READ_MS["ph"],
                "gas": SENSOR_READ_MS["gas"],
                "total": sensor_ms_fresh
            },
            "algorithm_ms": round(res_fresh["algorithm_ms"], 3),
            "result": {
                "centroid": round(res_fresh["centroid"], 3),
                "category": res_fresh["category"],
            },
            "sent_at_ms": int(time.time() * 1000)
        }
        print(f"[PY] Freshness → {payload_fresh}")
        publish(TOPIC_FRESH, payload_fresh)

        # ----- Pengawetan -----
        sensor_ms_presv = SENSOR_READ_MS["temp"] + SENSOR_READ_MS["hum"]
        res_presv = compute_preservation(temp_val, hum_val)
        payload_presv = {
            "type": "preservation",
            "inputs": {"temp": temp_val, "hum": hum_val},
            "sensor_read_ms": {
                "temp": SENSOR_READ_MS["temp"],
                "hum": SENSOR_READ_MS["hum"],
                "total": sensor_ms_presv
            },
            "algorithm_ms": round(res_presv["algorithm_ms"], 3),
            "result": {
                "centroid": round(res_presv["centroid"], 3),
                "category": res_presv["category"],
            },
            "sent_at_ms": int(time.time() * 1000)
        }
        print(f"[PY] Preservation → {payload_presv}")
        publish(TOPIC_PRESV, payload_presv)

        time.sleep(1.0)  # jeda antar-kiriman

    client.loop_stop()
    client.disconnect()

if __name__ == "__main__":
    main()
