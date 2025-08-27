## 📘 README – Python Publisher (`main.py`)

### Prasyarat

1. Python 3.9+
2. Virtual environment (opsional, tapi direkomendasikan)
3. Broker MQTT (pakai **Mosquitto** via `apt` atau Docker)

### Instalasi Broker MQTT

**Opsi 1 – Install langsung (Ubuntu/Debian):**

```bash
sudo apt update
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

**Opsi 2 – Pakai Docker (lebih portable):**

```bash
docker run -it --rm -p 1883:1883 eclipse-mosquitto
```

Broker default berjalan di `localhost:1883`.

### Jalankan Publisher

```bash
pip install -r requirements.txt
python main.py
```