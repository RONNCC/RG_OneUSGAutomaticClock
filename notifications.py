import sys
import subprocess

try:
    from plyer import notification as plyer_notification
except Exception:
    plyer_notification = None


def notify_user_with_ack(title: str, message: str, require_ack: bool = False) -> None:
    if require_ack and sys.platform == "darwin":
        try:
            title_escaped = title.replace("\\", "\\\\").replace("\"", "\\\"")
            message_escaped = message.replace("\\", "\\\\").replace("\"", "\\\"")
            script = f'tell application "System Events" to display alert "{title_escaped}" message "{message_escaped}" buttons {{"OK"}} default button "OK" as critical'
            result = subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return
        except Exception:
            pass
    if plyer_notification is not None:
        try:
            plyer_notification.notify(
                title=title,
                message=message,
                app_name="OneUSGAutomaticClock",
                timeout=10,
            )
        except Exception:
            pass
    print(message)