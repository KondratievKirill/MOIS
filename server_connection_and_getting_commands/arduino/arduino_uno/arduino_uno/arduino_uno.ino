#include <ArduinoJson.h>
#include "commands.h"

// ========== НАСТРОЙКИ ==========
const unsigned long TELEMETRY_INTERVAL = 10000;
unsigned long lastTelemetry = 0;

// ========== ФУНКЦИИ ==========

int freeMemory() {
  extern int __heap_start, *__brkval;
  int v;
  return (int) &v - (__brkval == 0 ? (int) &__heap_start : (int) __brkval);
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  
  Serial.println(F("{\"type\":\"system\",\"message\":\"Arduino starting\"}"));
  
  // Инициализация команд
  for (int i = 0; i < CMD_COUNT; i++) {
    CommandEntry cmd;
    memcpy_P(&cmd, &CMD_TABLE[i], sizeof(CommandEntry));
    
    if (cmd.init) {
      cmd.init();
    }
  }
  
  delay(100);
  sendCapabilities();
  
  Serial.print(F("{\"type\":\"system\",\"message\":\"Ready, RAM: "));
  Serial.print(freeMemory());
  Serial.println(F("\"}"));
}

void loop() {
  // Телеметрия
  if (millis() - lastTelemetry > TELEMETRY_INTERVAL) {
    sendTelemetry();
    lastTelemetry = millis();
  }
  
  // Обработка команд
  if (Serial.available() > 0) {
    String jsonInput = Serial.readStringUntil('\n');
    jsonInput.trim();
    
    if (jsonInput.length() > 0) {
      processCommand(jsonInput);
    }
  }
}

// ========== ОТПРАВКА CAPABILITIES ==========

void sendCapabilities() {
  Serial.println(F(
    "{\"type\":\"capabilities\",\"commands\":["
    
    // 1. ping
    "{\"name\":\"ping\",\"description\":\"Проверка связи\",\"params_schema\":{}},"
    
    // 2. motor
    "{\"name\":\"motor\",\"description\":\"Управление моторами\",\"params_schema\":{"
    "\"left_pwm\":{\"type\":\"integer\",\"min\":0,\"max\":255},"
    "\"right_pwm\":{\"type\":\"integer\",\"min\":0,\"max\":255},"
    "\"left_dir\":{\"type\":\"string\",\"enum\":[\"forward\",\"backward\"]},"
    "\"right_dir\":{\"type\":\"string\",\"enum\":[\"forward\",\"backward\"]}"
    "}},"
    
    // 3. duration
    "{\"name\":\"duration\",\"description\":\"Измерение расстояния ультразвуковым датчиком\",\"params_schema\":{}},"
    
    // 4. mpu6050 (ОДНА КОМАНДА!)
    "{\"name\":\"mpu6050\",\"description\":\"Все данные MPU6050 (гироскоп + акселерометр + температура)\",\"params_schema\":{}}"
    
    "]}"
  ));
  delay(100);
}

// ========== ТЕЛЕМЕТРИЯ ==========

void sendTelemetry() {
  Serial.print(F("{\"type\":\"telemetry\",\"data\":{\"uptime\":"));
  Serial.print(millis() / 1000);
  Serial.print(F(",\"free_memory\":"));
  Serial.print(freeMemory());
  Serial.println(F("}}"));
}

// ========== ОБРАБОТКА КОМАНД ==========

void processCommand(String& jsonStr) {
  StaticJsonDocument<256> doc;
  
  DeserializationError error = deserializeJson(doc, jsonStr);
  if (error) {
    Serial.println(F("{\"type\":\"error\",\"message\":\"JSON parse failed\"}"));
    return;
  }
  
  const char* cmd = doc["cmd"] | "";
  const char* commandId = doc["command_id"] | "";
  
  // Проверка на запрос capabilities
  if (strcmp(cmd, "get_capabilities") == 0) {
    Serial.println(F("{\"type\":\"system\",\"message\":\"Capabilities requested\"}"));
    delay(10);
    sendCapabilities();
    return;
  }
  
  // Поиск команды в таблице
  for (int i = 0; i < CMD_COUNT; i++) {
    CommandEntry entry;
    memcpy_P(&entry, &CMD_TABLE[i], sizeof(CommandEntry));
    
    char entryName[32];
    copyFlashString(entry.name, entryName, sizeof(entryName));
    
    if (strcmp(cmd, entryName) == 0) {
      unsigned long t0 = millis();
      CmdResult result = entry.handler(doc.as<JsonObject>());
      unsigned long execMs = millis() - t0;
      
      sendCommandResult(commandId, entryName, result.exitCode, result.output, result.error, execMs);
      return;
    }
  }
  
  Serial.print(F("{\"type\":\"error\",\"message\":\"Unknown command: "));
  Serial.print(cmd);
  Serial.println(F("\"}"));
}

// ========== РЕЗУЛЬТАТЫ ==========

void sendCommandResult(const char* commandId, const char* command, int exitCode, 
                       const String& stdOut, const String& stdErr, unsigned long execMs) {
  Serial.print(F("{\"type\":\"command_result\",\"command_id\":\""));
  Serial.print(commandId);
  Serial.print(F("\",\"command\":\""));
  Serial.print(command);
  Serial.print(F("\",\"exit_code\":"));
  Serial.print(exitCode);
  Serial.print(F(",\"stdout\":\""));
  Serial.print(stdOut);
  Serial.print(F("\""));
  
  if (stdErr.length() > 0) {
    Serial.print(F(",\"stderr\":\""));
    Serial.print(stdErr);
    Serial.print(F("\""));
  }
  
  Serial.print(F(",\"exec_time_ms\":"));
  Serial.print(execMs);
  Serial.println(F("}"));
}
