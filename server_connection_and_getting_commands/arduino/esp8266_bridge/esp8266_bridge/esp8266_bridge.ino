#include <ESP8266WiFi.h>
#include <WiFiClient.h>

const char* ssid = "S25_FE_user";
const char* password = "nmcw44ecvdcizs5";
const char* serverHost = "10.249.61.4";
const uint16_t serverPort = 5001;

WiFiClient client;
unsigned long lastReconnect = 0;
unsigned long lastHeartbeat = 0;
const unsigned long RECONNECT_INTERVAL = 5000;
const unsigned long HEARTBEAT_INTERVAL = 15000;

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);
  delay(1000);
  
  Serial.println("\n[ESP] WiFi Bridge Starting...");
  WiFi.begin(ssid, password);
  Serial.print("[WiFi] Connecting");
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] ✓ Connected!");
    Serial.print("[WiFi] IP: ");
    Serial.println(WiFi.localIP());
    digitalWrite(LED_BUILTIN, LOW);
  } else {
    Serial.println("\n[WiFi] ✗ Failed!");
  }
  
  connectToServer();
  Serial.println("[ESP] Ready");
}

void loop() {
  if (!client.connected()) {
    if (millis() - lastReconnect > RECONNECT_INTERVAL) {
      Serial.println("[ESP] Reconnecting...");
      connectToServer();
      lastReconnect = millis();
    }
  } else {
    digitalWrite(LED_BUILTIN, LOW);
    
    if (millis() - lastHeartbeat > HEARTBEAT_INTERVAL) {
      sendToServer("{\"type\":\"heartbeat\"}");
      lastHeartbeat = millis();
    }
    
    if (client.available()) {
      String line = client.readStringUntil('\n');
      line.trim();
      if (line.length() > 0) {
        Serial.println(line);
      }
    }
  }
  
  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0 && client.connected()) {
      client.println(line);
    }
  }
  
  delay(10);
}

void connectToServer() {
  Serial.print("[Server] Connecting to ");
  Serial.print(serverHost);
  Serial.print(":");
  Serial.println(serverPort);
  
  if (client.connect(serverHost, serverPort)) {
    Serial.println("[Server] ✓ Connected!");
    lastHeartbeat = millis();
    delay(100);
    sendToServer("{\"type\":\"hello\",\"device\":\"esp8266\"}");
  } else {
    Serial.println("[Server] ✗ Failed!");
  }
}

void sendToServer(String message) {
  if (client.connected()) {
    client.println(message);
  }
}
