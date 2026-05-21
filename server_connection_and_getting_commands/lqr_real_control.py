#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LQR Real-Time Control for WiFi Car
⚠️ ИСПРАВЛЕНИЕ: Инвертированная логика PWM (0=полная скорость, 255=стоп)
"""

import numpy as np
import matplotlib.pyplot as plt
from scipy.linalg import solve_discrete_are
from scipy import signal
import socketio
import time
import logging
import argparse
import json
from datetime import datetime

# ============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# ПАРАМЕТРЫ СИСТЕМЫ
# ============================================================================
W = 0.13          # Колея (м)
tau = 0.2         # Постоянная времени (с)
K_motor = 0.5     # Коэффициент мотора (м/с)
v0 = 0.3          # Номинальная скорость (м/с)

DT = 0.076        # Шаг дискретизации

u_nom = v0 / K_motor  # Номинальное управление (0.6)

# Параметры LQR
Q = np.diag([0.1, 0.1, 500, 100]) 
R = np.diag([50.0, 50.0])

# Параметры управления
PWM_MAX = 255
PWM_MIN = 0
U_MAX = 1.0
U_MIN = 0.0

# 🔥 ИНВЕРСИЯ PWM: True если 0=полная скорость, 255=стоп
PWM_INVERTED = True  # ← Установите True для инвертированной логики

# ============================================================================
# КЛАСС КОНТРОЛЛЕРА
# ============================================================================

class CarController:
    """Контроллер с инверсией PWM"""
    
    def __init__(self, server_url='http://localhost:5000'):
        self.sio = socketio.Client(logger=False, engineio_logger=False)
        self.server_url = server_url
        self.connected = False
        self.esp_connected = False
        self.commands_sent = 0
        self.commands_ack = 0
        
        self.sio.on('connect', self._on_connect)
        self.sio.on('disconnect', self._on_disconnect)
        self.sio.on('esp_status', self._on_esp_status)
        self.sio.on('command_result', self._on_command_result)
        
    def connect(self, timeout=10):
        logger.info(f"🔗 Connecting to {self.server_url}")
        try:
            self.sio.connect(self.server_url, wait_timeout=timeout)
            start = time.time()
            while not self.esp_connected and time.time() - start < timeout:
                time.sleep(0.1)
            if self.esp_connected:
                logger.info("✅ Connection chain: PC → Server → ESP → Arduino ✓")
            return self.esp_connected
        except Exception as e:
            logger.error(f"❌ Connection failed: {e}")
            return False
    
    def disconnect(self):
        if self.sio.connected:
            self.sio.disconnect()
    
    def _on_connect(self):
        logger.info("✓ WebSocket connected to Python server")
        self.connected = True
        
    def _on_disconnect(self):
        logger.info("✗ WebSocket disconnected")
        self.connected = False
        self.esp_connected = False
        
    def _on_esp_status(self, data):
        if data.get('connected'):
            logger.info(f"✓ ESP connected: {data.get('address')}")
            self.esp_connected = True
        else:
            logger.warning("✗ ESP disconnected")
            self.esp_connected = False
            
    def _on_command_result(self, data):
        self.commands_ack += 1
        cmd = data.get('command', 'unknown')
        exit_code = data.get('exit_code', -1)
        stdout = data.get('stdout', '')
        
        if exit_code == 0:
            logger.debug(f"✅ ACK [{self.commands_ack}]: {cmd} → {stdout}")
        else:
            logger.warning(f"⚠️ ERR [{self.commands_ack}]: {cmd} → {data.get('stderr', 'unknown')}")
    
    def send_motor_command(self, left_pwm, right_pwm, 
                          left_dir='forward', right_dir='forward',
                          command_id=None):
        """Отправка команды с инверсией PWM если нужно"""
        if not self.esp_connected:
            logger.warning("❌ Cannot send: ESP not connected")
            return False
        
        if command_id is None:
            command_id = f"lqr_{int(time.time()*1000)}"
        
        # 🔥 ИНВЕРСИЯ PWM: если True, то 255-pwm
        if PWM_INVERTED:
            left_pwm = PWM_MAX - left_pwm
            right_pwm = PWM_MAX - right_pwm
            logger.debug(f"🔄 PWM inverted: [{PWM_MAX-left_pwm}, {PWM_MAX-right_pwm}] → [{left_pwm}, {right_pwm}]")
        
        # Ограничение
        left_pwm = int(np.clip(left_pwm, PWM_MIN, PWM_MAX))
        right_pwm = int(np.clip(right_pwm, PWM_MIN, PWM_MAX))
        
        command = {
            'command': 'motor',
            'command_id': command_id,
            'params': {
                'left_pwm': left_pwm,
                'right_pwm': right_pwm,
                'left_dir': left_dir,
                'right_dir': right_dir
            }
        }
        
        self.commands_sent += 1
        logger.debug(f"📤 SEND [{self.commands_sent}]: pwm=[{left_pwm},{right_pwm}] dir=[{left_dir},{right_dir}]")
        
        try:
            self.sio.emit('send_command', command)
            return True
        except Exception as e:
            logger.error(f"❌ Send failed: {e}")
            return False
    
    def send_stop(self):
        """
        🔥 ОСТАНОВКА МОТОРОВ
        При инвертированной логике: PWM=255 = СТОП
        """
        logger.warning("🛑 EMERGENCY STOP - Sending multiple times...")
        
        # 🔥 При инвертированной логике: 255 = стоп, 0 = полная скорость!
        stop_pwm = PWM_MAX if PWM_INVERTED else PWM_MIN
        
        for i in range(3):
            success = self.send_motor_command(stop_pwm, stop_pwm, 
                                             left_dir='forward', right_dir='forward', 
                                             command_id=f"STOP_{i}")
            logger.warning(f"🛑 Stop command {i+1}/3 sent (pwm=[{stop_pwm},{stop_pwm}])")
            time.sleep(0.15)
        
        logger.warning("✅ All stop commands sent")
        return True
    
    def get_stats(self):
        return {
            'sent': self.commands_sent,
            'acked': self.commands_ack,
            'loss': self.commands_sent - self.commands_ack if self.commands_sent > 0 else 0
        }

# ============================================================================
# МОДЕЛЬ И LQR
# ============================================================================

def build_discrete_model(dt=DT):
    A_cont = np.array([
        [-1/tau, 0,     0,  0],
        [0,     -1/tau, 0,  0],
        [0,     0,      0,  v0],
        [-1/W,  1/W,    0,  0]
    ])
    
    B_cont = np.array([
        [K_motor/tau, 0],
        [0,           K_motor/tau],
        [0,           0],
        [0,           0]
    ])
    
    sys_d = signal.cont2discrete((A_cont, B_cont, np.eye(4), np.zeros((4,2))), 
                                  dt, method='zoh')
    return sys_d[0], sys_d[1]

def compute_lqr_gain(A_d, B_d, Q, R):
    P = solve_discrete_are(A_d, B_d, Q, R)
    K = np.linalg.inv(R + B_d.T @ P @ B_d) @ B_d.T @ P @ A_d
    return K

# ============================================================================
# ГРАФИКИ
# ============================================================================

def plot_results_graphs(data, LQR_Enabled, dt, T, u_nom=0.6, v0=0.3):
    t = np.array(data['time'])
    if len(t) == 0:
        logger.warning("⚠️ No data to plot")
        return
    
    # ГРАФИК 1: Скорости + PWM
    fig1, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6))
    fig1.suptitle(f'Скорости колёс (dt={dt}с, LQR={"ON" if LQR_Enabled else "OFF"}, PWM={"INVERTED" if PWM_INVERTED else "NORMAL"})', 
                  fontsize=14, fontweight='bold')
    
    ax1.plot(t, data['v_L'], 'b-o', label='v_L', markersize=2, linewidth=1)
    ax1.plot(t, data['v_R'], 'r-o', label='v_R', markersize=2, linewidth=1)
    ax1.axhline(y=v0, color='gray', linestyle='--', linewidth=1, label=f'v0={v0} м/с')
    ax1.set_ylabel('Скорость (м/с)')
    ax1.grid(True, alpha=0.3, linestyle=':')
    ax1.legend(loc='best', fontsize=9)
    ax1.axvspan(2.0, 2.2, alpha=0.15, color='orange', label='Возмущение')
    ax1.set_xlim([0, T])
    
    ax2.plot(t, data['pwm_L'], 'b-o', label='PWM_L', markersize=2, linewidth=1)
    ax2.plot(t, data['pwm_R'], 'r-o', label='PWM_R', markersize=2, linewidth=1)
    ax2.axhline(y=255 if PWM_INVERTED else 0, color='gray', linestyle=':', linewidth=1, label='Стоп')
    ax2.axhline(y=0 if PWM_INVERTED else 255, color='gray', linestyle='--', linewidth=1, label='Полная')
    ax2.set_ylabel('PWM (0-255)')
    ax2.set_xlabel('Время (с)')
    ax2.grid(True, alpha=0.3, linestyle=':')
    ax2.legend(loc='best', fontsize=9)
    ax2.set_xlim([0, T])
    
    plt.tight_layout()
    
    # ГРАФИК 2: Отклонения
    fig2, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6))
    fig2.suptitle(f'Отклонения от траектории', fontsize=14, fontweight='bold')
    
    ax1.plot(t, data['e_y'], 'g-o', markersize=2, linewidth=1.5)
    ax1.set_ylabel('Боковое отклонение (см)')
    ax1.grid(True, alpha=0.3, linestyle=':')
    ax1.axvspan(2.0, 2.2, alpha=0.15, color='orange')
    ax1.axhline(y=0, color='gray', linestyle=':', linewidth=0.5)
    ax1.set_xlim([0, T])
    
    ax2.plot(t, data['theta'], 'm-o', markersize=2, linewidth=1.5)
    ax2.set_ylabel('Угол θ (град)')
    ax2.set_xlabel('Время (с)')
    ax2.grid(True, alpha=0.3, linestyle=':')
    ax2.axhline(y=0, color='gray', linestyle=':', linewidth=0.5)
    ax2.set_xlim([0, T])
    
    plt.tight_layout()
    
    # ГРАФИК 3: Управление
    fig3, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(12, 8))
    fig3.suptitle(f'Управление и статистика', fontsize=14, fontweight='bold')
    
    ax1.plot(t, data['u_L'], 'b-o', label='u_L', markersize=2, linewidth=1)
    ax1.plot(t, data['u_R'], 'r-o', label='u_R', markersize=2, linewidth=1)
    ax1.axhline(y=u_nom, color='gray', linestyle='--', linewidth=1, label=f'u_nom={u_nom:.2f}')
    ax1.set_ylabel('Управление u (0-1)')
    ax1.grid(True, alpha=0.3, linestyle=':')
    ax1.legend(loc='best', fontsize=9)
    ax1.axvspan(2.0, 2.2, alpha=0.15, color='orange')
    ax1.set_xlim([0, T])
    ax1.set_ylim([0, 1.1])
    
    delta_v = np.array(data['v_R']) - np.array(data['v_L'])
    ax2.plot(t, delta_v, 'c-o', markersize=2, linewidth=1.5)
    ax2.set_ylabel('Δv (м/с)')
    ax2.grid(True, alpha=0.3, linestyle=':')
    ax2.axhline(y=0, color='gray', linestyle=':', linewidth=0.5)
    ax2.set_xlim([0, T])
    
    ax3.plot(t, data['commands_sent'], 'b-o', label='Отправлено', markersize=2, linewidth=1)
    ax3.plot(t, data['commands_acked'], 'g-o', label='Подтверждено', markersize=2, linewidth=1)
    ax3.set_ylabel('Команды')
    ax3.set_xlabel('Время (с)')
    ax3.grid(True, alpha=0.3, linestyle=':')
    ax3.legend(loc='best', fontsize=9)
    ax3.set_xlim([0, T])
    
    loss = np.array(data['commands_sent']) - np.array(data['commands_acked'])
    ax4.bar(t, loss, width=dt*0.7, color='orange', alpha=0.7, edgecolor='darkorange')
    ax4.set_ylabel('Потеряно')
    ax4.set_xlabel('Время (с)')
    ax4.grid(True, alpha=0.3, linestyle=':', axis='y')
    ax4.set_xlim([0, T])
    
    plt.tight_layout()
    plt.show()

# ============================================================================
# СИМУЛЯЦИЯ
# ============================================================================

def run_real_time_control(controller, LQR_Enabled=True, T=10.0, dt=DT, 
                         plot_results=True, save_data=False):
    
    A_d, B_d = build_discrete_model(dt)
    
    K_lqr = None
    if LQR_Enabled:
        K_lqr = compute_lqr_gain(A_d, B_d, Q, R)
        logger.info("✓ LQR gain computed")
    
    x = np.array([v0, v0, 0, 0])
    x_ref = np.array([v0, v0, 0, 0])
    
    # Возмущение 0.2 секунды
    impulse_start = 2.0
    impulse_end = 2.2
    impulse_mag = 2.0
    
    N = int(T / dt)
    data = {
        'time': [], 'v_L': [], 'v_R': [], 'e_y': [], 'theta': [],
        'u_L': [], 'u_R': [], 'pwm_L': [], 'pwm_R': [],
        'commands_sent': [], 'commands_acked': []
    }
    
    step = 0
    start_time = time.time()
    
    logger.info(f"🚀 Starting: T={T}s, dt={dt}s, steps={N}")
    logger.info(f"   LQR: {'ENABLED' if LQR_Enabled else 'DISABLED'}")
    logger.info(f"   PWM Logic: {'INVERTED (0=full, 255=stop)' if PWM_INVERTED else 'NORMAL (0=stop, 255=full)'}")
    logger.info(f"   Impulse: {impulse_start}s to {impulse_end}s ({impulse_end - impulse_start}s)")
    
    def progress_bar(current, total, width=40):
        percent = current / total
        filled = int(width * percent)
        bar = '█' * filled + '░' * (width - filled)
        return f"[{bar}] {current}/{total} ({percent*100:.1f}%)"
    
    try:
        while step < N:
            current_time = time.time() - start_time
            
            # Возмущение
            d = np.zeros(4)
            if impulse_start <= current_time < impulse_end:
                d[1] = impulse_mag * dt
            
            # Управление
            deviation = x - x_ref
            
            if LQR_Enabled and K_lqr is not None:
                u_correction = -K_lqr @ deviation
                u_total = np.array([u_nom, u_nom]) + u_correction
                u_total = np.clip(u_total, U_MIN, U_MAX)
            else:
                u_total = np.array([u_nom, u_nom])
            
            # 🔥 КОНВЕРТАЦИЯ В PWM (с учётом инверсии)
            if PWM_INVERTED:
                # Инвертированная логика: u=1 → pwm=0 (полная), u=0 → pwm=255 (стоп)
                left_pwm = (1.0 - u_total[0]) * PWM_MAX
                right_pwm = (1.0 - u_total[1]) * PWM_MAX
            else:
                # Обычная логика: u=1 → pwm=255 (полная), u=0 → pwm=0 (стоп)
                left_pwm = u_total[0] * PWM_MAX
                right_pwm = u_total[1] * PWM_MAX
            
            left_dir = 'forward' if u_total[0] >= 0 else 'backward'
            right_dir = 'forward' if u_total[1] >= 0 else 'backward'
            
            # Отправка
            command_id = f"lqr_{step:03d}"
            sent = controller.send_motor_command(
                left_pwm=left_pwm, right_pwm=right_pwm,
                left_dir=left_dir, right_dir=right_dir,
                command_id=command_id
            )
            
            if sent:
                logger.info(f"📡 [{step:4d}/{N}] t={current_time:.2f}s | "
                           f"u=[{u_total[0]:.2f},{u_total[1]:.2f}] | "
                           f"pwm=[{left_pwm:.0f},{right_pwm:.0f}] | "
                           f"e_y={x[2]*100:.1f}cm | "
                           f"{progress_bar(step+1, N)}")
            else:
                logger.warning(f"❌ [{step:4d}] Failed to send!")
            
            # Сбор данных
            data['time'].append(current_time)
            data['v_L'].append(x[0])
            data['v_R'].append(x[1])
            data['e_y'].append(x[2] * 100)
            data['theta'].append(np.degrees(x[3]))
            data['u_L'].append(u_total[0])
            data['u_R'].append(u_total[1])
            data['pwm_L'].append(left_pwm)
            data['pwm_R'].append(right_pwm)
            stats = controller.get_stats()
            data['commands_sent'].append(stats['sent'])
            data['commands_acked'].append(stats['acked'])
            
            # Обновление модели
            x = A_d @ x + B_d @ u_total + d
            
            # Синхронизация
            elapsed = time.time() - start_time
            expected_next = (step + 1) * dt
            sleep_time = expected_next - elapsed
            
            if sleep_time > 0.01:
                time.sleep(sleep_time)
            elif sleep_time < -dt:
                logger.warning(f"⚠️ Lag: {-sleep_time:.2f}s")
            
            step += 1
            
    except KeyboardInterrupt:
        logger.info("⌨️ Interrupted")
    finally:
        logger.info("🛑 Stopping motors...")
        controller.send_stop()
        time.sleep(0.5)
        logger.info("✅ Motors stopped")
    
    stats = controller.get_stats()
    logger.info(f"📊 Stats: sent={stats['sent']}, acked={stats['acked']}, loss={stats['loss']}")
    
    if plot_results and len(data['time']) > 0:
        logger.info("📈 Generating plots...")
        plot_results_graphs(data, LQR_Enabled, dt, T, u_nom, v0)
    
    if save_data:
        filename = f"lqr_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 Saved to {filename}")
    
    logger.info("✅ Finished")
    return data

# ============================================================================
# ЗАПУСК
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='LQR Real-Time Car Control')
    parser.add_argument('--server', default='http://localhost:5000')
    parser.add_argument('--time', type=float, default=10.0)
    parser.add_argument('--no-lqr', action='store_true')
    parser.add_argument('--dt', type=float, default=DT)
    parser.add_argument('--no-plot', action='store_true')
    parser.add_argument('--save', action='store_true')
    parser.add_argument('--debug', action='store_true')
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    dt = args.dt
    logger.info(f"⚙️ dt={dt}s, PWM={'INVERTED' if PWM_INVERTED else 'NORMAL'}")
    
    controller = CarController(server_url=args.server)
    
    try:
        if not controller.connect(timeout=15):
            logger.error("❌ Connection failed")
            return 1
        
        logger.info("✅ Ready! Press ENTER to start")
        input("🎯 Press ENTER...")
        
        data = run_real_time_control(
            controller=controller,
            LQR_Enabled=not args.no_lqr,
            T=args.time,
            dt=dt,
            plot_results=not args.no_plot,
            save_data=args.save
        )
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        controller.disconnect()
    
    return 0

if __name__ == '__main__':
    exit(main())