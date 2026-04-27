"""
Notification utilities using ntfy.sh
"""
import requests
import yaml
from pathlib import Path

# Project root
project_dir = Path(__file__).parent.parent.parent


def load_notification_config():
    """Load notification config from config.yaml"""
    config_file = project_dir / "config/config.yaml"
    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
            return config.get('notifications', {})
    except Exception:
        return {}


def send_ntfy_alert(message: str, title: str = "Hedge Bot Alert", priority: str = "default", tags: list = None):
    """
    Send alert via ntfy.sh
    
    Args:
        message: Alert message
        title: Alert title (default: "Hedge Bot Alert")
        priority: Priority level (default, low, high, urgent)
        tags: List of tags (e.g. ["rotating_light", "warning"])
    
    Returns:
        bool: True if successful, False otherwise
    """
    config = load_notification_config()
    topic = config.get('ntfy_topic')
    
    if not topic:
        # No topic configured, skip
        return False
    
    try:
        # ntfy.sh URL
        url = f"https://ntfy.sh/{topic}"
        
        # Headers (encode properly for HTTP)
        headers = {
            "Title": title.encode('utf-8').decode('latin-1', errors='ignore'),
            "Priority": priority
        }
        
        # Add tags if provided
        if tags:
            headers["Tags"] = ", ".join(tags)
        
        # Send POST request (message already encoded as utf-8)
        response = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=5)
        
        if response.status_code == 200:
            return True
        else:
            return False
    except Exception as e:
        print(f"Error sending ntfy alert: {e}")
        return False


def send_bot_alert(symbol: str, event: str, details: str = ""):
    """
    Send bot-specific alert
    
    Args:
        symbol: Trading symbol (e.g. "SYMBOLUSDT")
        event: Event type (e.g. "started", "stopped", "burn", "rebuy")
        details: Additional details
    """
    event_titles = {
        "started": "🚀 Bot gestartet",
        "stopped": "⏹️ Bot gestoppt",
        "burn": "🔥 Burn abgeschlossen",
        "rebuy": "⚡ Rebuy ausgelöst",
        "error": "❌ Fehler",
        "warning": "⚠️ Warnung"
    }
    
    event_tags = {
        "started": ["green_circle", "rocket"],
        "stopped": ["red_circle", "stop_sign"],
        "burn": ["fire", "money_with_wings"],
        "rebuy": ["zap", "chart_increasing"],
        "error": ["rotating_light", "warning"],
        "warning": ["warning", "exclamation"]
    }
    
    title = event_titles.get(event, f"Bot Event: {event}")
    tags = event_tags.get(event, [])
    # Set all important events to "urgent" or "high" so phone rings
    priority = "urgent" if event in ["error", "rebuy", "burn", "stopped", "started"] else "high"
    
    message = f"Symbol: {symbol}\n"
    if details:
        message += f"{details}"
    
    return send_ntfy_alert(message, title=title, priority=priority, tags=tags)

