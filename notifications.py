import sys
import subprocess


def _osascript_notify(title: str, message: str, require_ack: bool) -> bool:
    """Try macOS-native notification via osascript. Returns True on success."""
    if sys.platform != "darwin":
        return False
    try:
        title_escaped = title.replace("\\", "\\\\").replace("\"", "\\\"")
        message_escaped = message.replace("\\", "\\\\").replace("\"", "\\\"")
        if require_ack:
            script = f'tell application "System Events" to display alert "{title_escaped}" message "{message_escaped}" buttons {{"OK"}} default button "OK" as critical'
        else:
            script = f'display notification "{message_escaped}" with title "{title_escaped}"'
        result = subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def notify_user_with_ack(title: str, message: str, require_ack: bool = False) -> None:
    if _osascript_notify(title, message, require_ack):
        return
    # Fallback: plyer (skip on macOS to avoid noisy pyobjus import errors)
    if sys.platform != "darwin":
        try:
            from plyer import notification as plyer_notification
            plyer_notification.notify(
                title=title,
                message=message,
                app_name="OneUSGAutomaticClock",
                timeout=10,
            )
            return
        except Exception:
            pass
    print(message)