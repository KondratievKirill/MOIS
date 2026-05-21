#include <Arduino.h>
#include <ArduinoJson.h>
#include "commands.h"

#define TRIG_PIN 12
#define ECHO_PIN 13

void initDuration() {
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);  // Исходное состояние
}

CmdResult handleDuration(JsonObject params) {
  // Отправляем импульс 10 мкс
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  
  // Читаем длительность (таймаут 30 мс = 30000 мкс)
  unsigned long duration = pulseIn(ECHO_PIN, HIGH, 30000UL);
  
  // Проверяем таймаут
  if (duration == 0) {
    return CmdResult(1, "", "Timeout - no echo received");
  }
  

  
  // Формируем результат
  String out = "Duration: ";
  out += duration;

  
  return CmdResult(0, out, "");
}
