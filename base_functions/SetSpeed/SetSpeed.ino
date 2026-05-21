int left_ctrl = 2;
int left_pwm = 5;
int right_ctrl = 4;
int right_pwm = 6;

void setup() {
  Serial.begin(9600);
  pinMode(left_ctrl, OUTPUT);
  pinMode(left_pwm, OUTPUT);
  pinMode(right_ctrl, OUTPUT);
  pinMode(right_pwm, OUTPUT);

}

void setSpeed(double speed, bool reverseDirection = 0){
  double pwm = (1 - 1.003 * speed) * 255;
  Serial.println(pwm);
  if (reverseDirection){
    digitalWrite(left_ctrl, LOW);
    analogWrite(left_pwm, (int)pwm);
    digitalWrite(right_ctrl, LOW);
    analogWrite(right_pwm, (int)pwm);
    return;
  }
  digitalWrite(left_ctrl, HIGH);
  analogWrite(left_pwm, (int)pwm);
  digitalWrite(right_ctrl, HIGH);
  analogWrite(right_pwm, (int)pwm);

}

void loop() {
  setSpeed(1);

}


