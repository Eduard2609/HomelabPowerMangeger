from flask import Flask, render_template, request, jsonify, send_from_directory
import time
import threading
import datetime
import json
import os
import subprocess
import socket
import paramiko
import logging
from logging.handlers import RotatingFileHandler
import csv
import urllib.request

app = Flask(__name__)

# Setup logging
log_dir = 'logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# System log for debugging and errors
system_log_handler = RotatingFileHandler(
    os.path.join(log_dir, 'system.log'),
    maxBytes=1024 * 1024 * 5,  # 5 MB
    backupCount=5
)
system_log_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
))

system_logger = logging.getLogger('system_logger')
system_logger.setLevel(logging.INFO)
system_logger.addHandler(system_log_handler)

# Activity log for user actions and device status changes
def log_activity(action, result, details=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file = os.path.join(log_dir, 'activity.csv')

    # Create the file with headers if it doesn't exist
    if not os.path.exists(log_file):
        with open(log_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Timestamp', 'Action', 'Result', 'Details'])

    # Append the new activity
    with open(log_file, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, action, result, details])

# Configuration
CONFIG_FILE = 'config.json'

# Default configuration
default_config = {
    'state': False,
    'scheduled_off_time': None,
    'restricted_hours': {
        'start': 2,  # 2 AM
        'end': 7     # 7 AM
    },
    'target_device': {
        'mac_address': '2c:f0:5d:75:bc:b6',  # Change this to your device's MAC
        'ip_address': '172.26.1.31',       # Change this to your device's IP
        'ssh_port': 22,
        'ssh_username': 'eduard',
        'ssh_key_path': '/home/eduard/.ssh/id_ed25519', # Store the path to the key here
        'ssh_key_passphrase': os.environ.get('SSH_KEY_PASSPHRASE', '')  # Get from environment, default to empty string
    },
    'plex': {
        'tautulli_ip': '',       # Your Tautulli server IP
        'tautulli_port': 7979,   # Your Tautulli port
        'tautulli_apikey': ''    # Your Tautulli API key
    }
}

# Load or create configuration
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            system_logger.error(f"Error parsing {CONFIG_FILE}. Using default config.")
            return default_config.copy()
    else:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f)
        system_logger.info(f"Created new configuration file: {CONFIG_FILE}")
        return default_config.copy()

# Save configuration
def save_config(config):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        system_logger.error(f"Failed to save configuration: {str(e)}")

config = load_config()
off_timer = None

def ensure_config_defaults():
    # Add missing keys to existing configs without overwriting user values
    changed = False
    if 'pc_device' not in config:
        config['pc_device'] = {
            'mac_address': '34:5A:60:1C:CD:9F',
            'ip_address': '172.26.1.26'
        }
        changed = True
    if 'plex' not in config or 'tautulli_ip' not in config.get('plex', {}):
        config['plex'] = {
            'tautulli_ip': '',
            'tautulli_port': 7979,
            'tautulli_apikey': ''
        }
        changed = True
    if changed:
        save_config(config)

ensure_config_defaults()

# ---------------------------------------------------------------------------
# Plex stream cache — avoids hitting the Plex API on every 5-second poll.
# Cached for PLEX_CACHE_TTL seconds; stale on error so the UI keeps showing
# the last known state rather than flashing empty.
# ---------------------------------------------------------------------------
PLEX_CACHE_TTL = 10  # seconds
_plex_cache = {
    'streams': [],
    'fetched_at': 0
}

def _fetch_plex_streams():
    """Query Tautulli get_activity endpoint and return a list of active streams."""
    plex_cfg = config.get('plex', {})
    ip = plex_cfg.get('tautulli_ip', '').strip()
    port = plex_cfg.get('tautulli_port', 7979)
    apikey = plex_cfg.get('tautulli_apikey', '').strip()

    if not ip or not apikey:
        return []  # Not configured — silently return empty

    url = f'http://{ip}:{port}/api/v2?cmd=get_activity&apikey={apikey}'

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        system_logger.warning(f"Tautulli API request failed: {e}")
        return None  # None signals "use stale cache"

    if data.get('response', {}).get('result') != 'success':
        system_logger.warning(f"Tautulli returned non-success: {data}")
        return None

    sessions = data.get('response', {}).get('data', {}).get('sessions', [])
    streams = []
    for s in sessions:
        media_type = s.get('media_type', 'unknown')
        title = s.get('title', 'Unknown')
        grandparent_title = s.get('grandparent_title', '')
        user = s.get('friendly_name', '')
        resolution = s.get('video_full_resolution', '')
        state = s.get('state', '')

        # For episodes: "Show — Episode Title"
        if media_type == 'episode' and grandparent_title:
            display_title = f"{grandparent_title} \u2014 {title}"
        else:
            display_title = title

        streams.append({
            'title': display_title,
            'type': media_type,
            'user': user,
            'quality': resolution,
            'state': state
        })

    return streams

def get_plex_streams():
    """Return cached streams, refreshing the cache when TTL has expired."""
    global _plex_cache
    now = time.time()

    if now - _plex_cache['fetched_at'] >= PLEX_CACHE_TTL:
        result = _fetch_plex_streams()
        if result is not None:  # None means fetch failed — keep stale data
            _plex_cache['streams'] = result
        _plex_cache['fetched_at'] = now

    return _plex_cache['streams']

def _ping(ip):
    try:
        if os.name == 'nt':
            return subprocess.run(['ping', '-n', '1', '-w', '1000', ip],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        else:
            return subprocess.run(['ping', '-c', '1', '-W', '1', ip],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False

def _broadcast_for_ip(ip):
    try:
        parts = ip.split('.')
        if len(parts) == 4:
            parts[3] = '255'
            return '.'.join(parts)
    except Exception:
        pass
    return '255.255.255.255'

def send_wol(mac_address, broadcast_ip='255.255.255.255', port=9):
    mac_clean = mac_address.replace(':', '').replace('-', '').lower()
    if len(mac_clean) != 12:
        raise ValueError('Invalid MAC address')
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b'\xff' * 6 + (mac_bytes * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = {broadcast_ip, '255.255.255.255'}
        for target in targets:
            try:
                s.sendto(packet, (target, port))
            except Exception as e:
                system_logger.warning(f"WoL send error to {target}:{port}: {str(e)}")

def check_restricted_hours():
    now = datetime.datetime.now()
    start = config['restricted_hours']['start']
    end = config['restricted_hours']['end']
    current_hour = now.hour

    return start <= current_hour < end

def check_device_status():
    """Check if the device is reachable via ping"""
    try:
        ip = config['target_device']['ip_address']
        is_online = _ping(ip)

        # Log state changes
        current_state = config.get('state', False)
        if current_state != is_online:
            log_activity('state_change', 'success',
                         f"Device state changed from {current_state} to {is_online}")

        return is_online
    except Exception as e:
        system_logger.error(f"Error checking device status: {str(e)}")
        return False

def check_device_status_ip(ip):
    try:
        return _ping(ip)
    except Exception as e:
        system_logger.error(f"Error checking device status for {ip}: {str(e)}")
        return False

def wake_on_lan():
    """Send Wake-on-LAN magic packet"""
    try:
        mac = config['target_device']['mac_address']
        ip = config['target_device']['ip_address']
        bcast = _broadcast_for_ip(ip)
        system_logger.info(f"Sending WoL packet to {mac} via {bcast}")
        send_wol(mac, bcast)

        # Give the device some time to boot
        time.sleep(2)

        # Check if wake-up was successful
        attempts = 0
        max_attempts = 5
        while attempts < max_attempts:
            is_online = check_device_status()
            if is_online:
                system_logger.info(f"Device is now online after WoL")
                config['state'] = True
                save_config(config)
                log_activity('wake_on_lan', 'success', f"Device woken up after {attempts+1} attempts")
                return True

            system_logger.info(f"Waiting for device to wake up (attempt {attempts+1}/{max_attempts})")
            time.sleep(2)
            attempts += 1

        system_logger.warning(f"Failed to wake device after {max_attempts} attempts")
        log_activity('wake_on_lan', 'failed', f"Device failed to respond after {max_attempts} attempts")
        return False
    except Exception as e:
        system_logger.error(f"Error sending WoL packet: {str(e)}")
        log_activity('wake_on_lan', 'error', str(e))
        return False

def wake_pc():
    """Wake the PC device using MAC/IP from config['pc_device']"""
    try:
        mac = config['pc_device']['mac_address']
        ip = config['pc_device']['ip_address']
        bcast = _broadcast_for_ip(ip)
        system_logger.info(f"Sending PC WoL packet to {mac} via {bcast}")
        send_wol(mac, bcast)
        time.sleep(2)
        attempts = 0
        max_attempts = 5
        while attempts < max_attempts:
            is_online = check_device_status_ip(ip)
            if is_online:
                log_activity('pc_wake_on_lan', 'success', f"PC at {ip} is online after {attempts+1} attempts")
                return True
            time.sleep(2)
            attempts += 1
        log_activity('pc_wake_on_lan', 'failed', f"PC at {ip} failed to respond after {max_attempts} attempts")
        return False
    except Exception as e:
        system_logger.error(f"Error sending PC WoL packet: {str(e)}")
        log_activity('pc_wake_on_lan', 'error', str(e))
        return False

def shutdown_device():
    """Shutdown the device via SSH"""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        system_logger.info("Attempting SSH connection for shutdown")
        ssh_config = config['target_device']

        try:
            # Load the private key, handling passphrase if necessary
            key = paramiko.Ed25519Key.from_private_key_file(
                ssh_config['ssh_key_path'],
                password=ssh_config['ssh_key_passphrase']
            )
            system_logger.info("Key loaded successfully")
        except paramiko.ssh_exception.PasswordRequiredException:
            system_logger.error("SSH key requires a passphrase but none was provided.")
            log_activity('shutdown', 'error', "SSH key requires a passphrase but none was provided.")
            return False
        except Exception as e:
            system_logger.error(f"Error loading SSH key: {str(e)}")
            log_activity('shutdown', 'error', f"Error loading SSH key: {str(e)}")
            return False

        try:
            ssh.connect(
                ssh_config['ip_address'],
                port=ssh_config['ssh_port'],
                username=ssh_config['ssh_username'],
                pkey=key,  # Use the loaded key instead of key_filename
                timeout=5
            )
            system_logger.info("SSH connection successful")

            # Send shutdown command
            system_logger.info("Executing shutdown command")
            ssh.exec_command('systemctl suspend --no-wall')
            ssh.close()
            system_logger.info("Shutdown command executed successfully")

            # Update state
            config['state'] = False
            save_config(config)
            log_activity('shutdown', 'success', "Shutdown command sent successfully")
            return True

        except Exception as connect_err:
            system_logger.error(f"SSH Connect Error: {str(connect_err)}")
            log_activity('shutdown', 'error', f"SSH Connect Error: {str(connect_err)}")
            return False
    except Exception as e:
        system_logger.error(f"General SSH Error during shutdown: {str(e)}")
        log_activity('shutdown', 'error', str(e))
        return False

def schedule_off(hours):
    global off_timer
    if off_timer:
        off_timer.cancel()
        system_logger.info("Cancelled previous scheduled shutdown")

    def turn_off():
        system_logger.info(f"Executing scheduled shutdown after {hours} hours")
        shutdown_device()
        config['scheduled_off_time'] = None
        save_config(config)

    seconds = hours * 3600
    off_timer = threading.Timer(seconds, turn_off)
    off_timer.daemon = True
    off_timer.start()

    # Save scheduled time
    scheduled_time = datetime.datetime.now() + datetime.timedelta(hours=hours)
    config['scheduled_off_time'] = scheduled_time.strftime("%Y-%m-%d %H:%M:%S")
    save_config(config)

    system_logger.info(f"Scheduled shutdown in {hours} hours ({scheduled_time})")
    log_activity('schedule', 'success', f"Scheduled shutdown in {hours} hours")

@app.route('/')
def index():
    system_logger.info("Web interface accessed")
    log_activity('page_visit', 'success', "Main page loaded")
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(app.root_path, 'manifest.json')

@app.route('/api/status', methods=['GET'])
def get_status():
    # Check the actual device status
    is_online = check_device_status()
    if is_online != config['state']:
        config['state'] = is_online
        save_config(config)

    # Check if we're in restricted hours
    in_restricted_hours = check_restricted_hours()

    return jsonify({
        'state': config['state'],
        'scheduled_off_time': config['scheduled_off_time'],
        'in_restricted_hours': in_restricted_hours
    })

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    action = data.get('action')
    client_ip = request.remote_addr

    system_logger.info(f"Control action '{action}' requested from {client_ip}")

    if action == 'turn_on':
        # Check if we're in restricted hours
        if check_restricted_hours():
            system_logger.warning(f"Turn on attempt during restricted hours from {client_ip}")
            log_activity('turn_on', 'denied', "Attempted during restricted hours")
            return jsonify({
                'success': False,
                'message': 'Cannot turn on during restricted hours (2 AM - 7 AM)'
            })

        # Try to wake up the device
        success = wake_on_lan()

        # Cancel any scheduled off timer
        global off_timer
        if off_timer and success:
            off_timer.cancel()
            config['scheduled_off_time'] = None
            save_config(config)
            system_logger.info("Cancelled scheduled shutdown after device wake-up")

        return jsonify({'success': success, 'state': config['state']})

    elif action == 'turn_off':
        success = shutdown_device()

        # Cancel any scheduled off timer
        if off_timer:
            off_timer.cancel()
            config['scheduled_off_time'] = None
            save_config(config)
            system_logger.info("Cancelled scheduled shutdown after manual shutdown")

        return jsonify({'success': success, 'state': config['state']})

    elif action == 'schedule_off':
        hours = int(data.get('hours', 1))
        schedule_off(hours)
        return jsonify({'success': True, 'scheduled_off_time': config['scheduled_off_time']})

    system_logger.warning(f"Invalid action '{action}' requested from {client_ip}")
    log_activity('control', 'invalid', f"Invalid action: {action}")
    return jsonify({'success': False, 'message': 'Invalid action'})

@app.route('/api/pc/wake', methods=['POST'])
def pc_wake():
    client_ip = request.remote_addr
    system_logger.info(f"PC wake requested from {client_ip}")
    success = wake_pc()
    return jsonify({'success': success})

@app.route('/api/pc/status', methods=['GET'])
def pc_status():
    try:
        ip = config['pc_device']['ip_address']
        online = check_device_status_ip(ip)
        return jsonify({'online': online})
    except Exception as e:
        system_logger.error(f"PC status error: {str(e)}")
        return jsonify({'online': False, 'error': str(e)}), 500

@app.route('/api/plex/streams', methods=['GET'])
def plex_streams():
    streams = get_plex_streams()
    return jsonify(streams)

@app.route('/api/logs', methods=['GET'])
def get_logs():
    log_type = request.args.get('type', 'activity')
    limit = int(request.args.get('limit', 100))

    if log_type == 'activity':
        log_file = os.path.join(log_dir, 'activity.csv')
        if not os.path.exists(log_file):
            return jsonify([])

        logs = []
        try:
            with open(log_file, 'r') as f:
                reader = csv.reader(f)
                headers = next(reader)  # Skip header row

                # Get all rows and take the most recent ones
                all_rows = list(reader)
                rows = all_rows[-limit:] if len(all_rows) > limit else all_rows

                for row in rows:
                    if len(row) >= 4:
                        logs.append({
                            'timestamp': row[0],
                            'action': row[1],
                            'result': row[2],
                            'details': row[3]
                        })

        except Exception as e:
            system_logger.error(f"Error reading activity logs: {str(e)}")
            return jsonify({'error': str(e)})

        return jsonify(logs)

    elif log_type == 'system':
        log_file = os.path.join(log_dir, 'system.log')
        if not os.path.exists(log_file):
            return jsonify([])

        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                # Take the most recent lines
                lines = lines[-limit:] if len(lines) > limit else lines
                return jsonify(lines)
        except Exception as e:
            system_logger.error(f"Error reading system logs: {str(e)}")
            return jsonify({'error': str(e)})

    return jsonify({'error': 'Invalid log type'})

# Log application startup
system_logger.info("Application starting up")
log_activity('startup', 'success', "Application initialized")

# ---------------------------------------------------------------------------
# Suppress noisy 400 logs from TLS scanners hitting the plain-HTTP port.
# Werkzeug logs these at WARNING level through the standard 'werkzeug' logger;
# we install a filter that drops any message containing the TLS ClientHello
# signature byte sequence so they never reach stderr.
# ---------------------------------------------------------------------------
class _TLSScannerFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # TLS ClientHello always starts with \x16\x03 after the raw bytes
        # are decoded into the log string; also catch the 'Bad request version' prefix
        if 'Bad request version' in msg:
            return False
        if '\x16\x03' in msg:
            return False
        return True

logging.getLogger('werkzeug').addFilter(_TLSScannerFilter())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
