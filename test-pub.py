# test-pub.py
import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect("127.0.0.1", 1883, 30)
c.loop_start()
c.publish("fish/fuzzy/test", "ping", qos=1)
c.loop_stop(); c.disconnect()
