import os
import re
import time
import json
import threading
import sys
import ctypes
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    import customtkinter as ctk
    import pystray
    from PIL import Image
    from send2trash import send2trash
    from plyer import notification
except ImportError:
    ctypes.windll.user32.MessageBoxW(
        0,
        "Missing libraries!\n\nRun:\n  pip install customtkinter pystray pillow send2trash plyer",
        "TXT Timer - Error",
        0x10
    )
    sys.exit(1)


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


DESKTOP  = Path.home() / "Desktop"
DATA_DIR = Path.home() / "AppData" / "Local" / "TxtTimer"
DATA_FILE = DATA_DIR / "data.json"
LOG_FILE  = DATA_DIR / "log.txt"

config = {
    "hard_delete":     False,
    "notifications":   True,
    "command":         "del",
    "scan_interval":   5,
    "extend_presets":  [5, 15, 30],
}

scheduled    = {}
paused       = {}
lock         = threading.Lock()
running      = True
is_active    = True
list_widgets = {}


def log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line  = f"[{stamp}] {msg}"
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log("CRITICAL CRASH:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


sys.excepthook = handle_exception


def send_os_notification(msg):
    if not config.get("notifications", True):
        return
    def _notify():
        try:
            notification.notify(title="TXT Timer", message=msg,
                                app_name="TXT Timer", timeout=5)
        except Exception as e:
            log(f"NOTIFY ERROR: {e}")
    threading.Thread(target=_notify, daemon=True).start()


def load_data():
    global config
    if not DATA_FILE.exists():
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, dict) and "settings" in raw:
            config.update(raw["settings"])
            tasks        = raw.get("scheduled", {})
            saved_paused = raw.get("paused", {})
        else:
            tasks        = raw if isinstance(raw, dict) else {}
            saved_paused = {}

        now = datetime.now()
        with lock:
            for fname, ts in tasks.items():
                try:
                    if fname in saved_paused:
                        remaining        = int(saved_paused[fname])
                        scheduled[fname] = now + timedelta(seconds=remaining)
                        paused[fname]    = remaining
                        log(f"LOADED PAUSED '{fname}' ({remaining}s left)")
                    else:
                        dt = datetime.fromisoformat(ts)
                        if dt > now:
                            scheduled[fname] = dt
                            log(f"LOADED '{fname}'")
                        else:
                            execute_deletion(fname)
                except Exception:
                    continue
    except Exception as e:
        log(f"WARN failed to load data — {e}")


def save_data():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with lock:
            safe_scheduled = {k: v.isoformat() for k, v in scheduled.items()}
            safe_paused    = dict(paused)
            safe_config    = dict(config)
        data = {
            "settings":  safe_config,
            "scheduled": safe_scheduled,
            "paused":    safe_paused,
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"ERROR saving data — {e}")


def execute_deletion(fname):
    fp = DESKTOP / fname
    if not fp.exists():
        log(f"NOT FOUND '{fname}'")
        return
    try:
        if config.get("hard_delete", False):
            fp.unlink()
            verb = "permanently deleted"
        else:
            send2trash(str(fp))
            verb = "moved to recycle bin"
        log(f"ACTION: '{fname}' {verb}")
        send_os_notification(f"'{fname}' has been {verb}.")
    except Exception as e:
        log(f"DELETE ERROR on '{fname}' — {e}")



def parse_delay(filename):
    try:
        cmd     = config.get("command", "del")
        pattern = rf'{re.escape(cmd)}\((\d+(?:[.,]\d+)?)\)'
        m       = re.search(pattern, filename, re.IGNORECASE)
        if not m:
            return None
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def parse_extend_presets(raw_str=None):
    if raw_str is not None:
        val = raw_str
    else:
        val = config.get("extend_presets", [5, 15, 30])
    if isinstance(val, list):
        return [int(x) for x in val if str(x).strip().isdigit() or isinstance(x, int)]
    try:
        return [int(x.strip()) for x in str(val).split(",") if x.strip().isdigit()]
    except Exception:
        return [5, 15, 30]


def pause_timer(fname):
    with lock:
        if fname not in scheduled or fname in paused:
            return
        remaining     = max(0, int((scheduled[fname] - datetime.now()).total_seconds()))
        paused[fname] = remaining
    save_data()
    log(f"PAUSED '{fname}' ({remaining}s left)")


def resume_timer(fname):
    with lock:
        if fname not in paused:
            return
        remaining         = paused.pop(fname)
        scheduled[fname]  = datetime.now() + timedelta(seconds=remaining)
    save_data()
    log(f"RESUMED '{fname}'")


def toggle_pause_timer(fname):
    with lock:
        is_paused = fname in paused
    if is_paused:
        resume_timer(fname)
    else:
        pause_timer(fname)


def extend_timer(fname, minutes):
    with lock:
        if fname in paused:
            paused[fname] += minutes * 60
        elif fname in scheduled:
            scheduled[fname] += timedelta(minutes=minutes)
        else:
            return
    save_data()
    log(f"EXTENDED '{fname}' +{minutes}m")


def cancel_timer(fname):
    with lock:
        scheduled.pop(fname, None)
        paused.pop(fname, None)
    save_data()
    log(f"CANCELLED '{fname}'")


def scan_once():
    if not DESKTOP.is_dir():
        return
    try:
        txts = {f.name for f in DESKTOP.glob("*.txt")}
    except Exception as e:
        log(f"DESKTOP READ ERROR: {e}")
        return

    now     = datetime.now()
    changed = False

    with lock:
        for fname in txts:
            if fname not in scheduled:
                delay = parse_delay(fname)
                if delay is not None:
                    scheduled[fname] = now + timedelta(minutes=delay)
                    changed = True
                    log(f"QUEUED '{fname}'")

        expired = [f for f, dt in list(scheduled.items())
                   if now >= dt and f not in paused]
        for fname in expired:
            execute_deletion(fname)
            scheduled.pop(fname, None)
            changed = True

        orphans = [f for f in list(scheduled.keys()) if f not in txts]
        for fname in orphans:
            log(f"CANCELLED (orphan) '{fname}'")
            scheduled.pop(fname, None)
            paused.pop(fname, None)
            changed = True

    if changed:
        save_data()


def scanner_loop():
    while running:
        if is_active:
            try:
                scan_once()
            except Exception as e:
                log(f"SCAN LOOP ERROR: {e}")
        interval = max(1, config.get("scan_interval", 5))
        for _ in range(interval * 4):
            if not running:
                break
            time.sleep(0.25)



def _fmt_secs(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _remaining_str(dt):
    return _fmt_secs((dt - datetime.now()).total_seconds())


def _time_color(dt):
    secs = int((dt - datetime.now()).total_seconds())
    if secs > 600:   return "#9CA3AF"
    elif secs > 120: return "#F59E0B"
    else:            return "#EF4444"


def _ext_label(minutes):
    """Pretty label for extend button: 60→+1h, 90→+1h30m, 15→+15m"""
    if minutes >= 60 and minutes % 60 == 0:
        return f"+{minutes // 60}h"
    elif minutes >= 60:
        return f"+{minutes // 60}h{minutes % 60}m"
    return f"+{minutes}m"


def get_icon_image():
    try:
        return Image.open(resource_path("txtTimerLogo.ico"))
    except Exception:
        return Image.new("RGBA", (64, 64), (0, 0, 0, 0))


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

app = ctk.CTk()
app.title("TXT Timer")
app.geometry("900x570")
app.resizable(False, False)
app.configure(fg_color="#4F5268")
app.protocol("WM_DELETE_WINDOW", lambda: app.withdraw())

top_bar = ctk.CTkFrame(app, height=60, fg_color="#18181A", corner_radius=0)
top_bar.pack(fill="x", side="top")
top_bar.pack_propagate(False)

ctk.CTkLabel(top_bar, text="TXT Timer Dashboard",
             font=("Segoe UI", 18, "bold"), text_color="#FFFFFF").pack(side="left", padx=20)

btn_toggle = ctk.CTkButton(top_bar, text="⏹ Stop System",
                            font=("Segoe UI", 13, "bold"),
                            fg_color="#C8102E", hover_color="#990C23",
                            corner_radius=6, height=36, width=130)
btn_toggle.pack(side="right", padx=20)

main_area = ctk.CTkFrame(app, fg_color="transparent")
main_area.pack(fill="both", expand=True, padx=25, pady=20)


col_left = ctk.CTkFrame(main_area, fg_color="transparent", width=350)
col_left.pack(side="left", fill="y")
col_left.pack_propagate(False)

header_left = ctk.CTkFrame(col_left, fg_color="transparent")
header_left.pack(fill="x", pady=(0, 12))
ctk.CTkLabel(header_left, text="⚙", font=("Segoe UI", 16), text_color="#60A5FA").pack(side="left", padx=(0, 8))
ctk.CTkLabel(header_left, text="Configuration", font=("Segoe UI", 15, "bold"), text_color="#FFFFFF").pack(side="left")

card_left = ctk.CTkFrame(col_left, fg_color="#1F2025", corner_radius=12)
card_left.pack(fill="both", expand=True)

container_left = ctk.CTkFrame(card_left, fg_color="transparent")
container_left.pack(fill="both", expand=True, padx=22, pady=18)

row1 = ctk.CTkFrame(container_left, fg_color="transparent")
row1.pack(fill="x", pady=(0, 14))

icon_trash_frame = ctk.CTkFrame(row1, width=36, height=36, corner_radius=8, fg_color="#4A1522")
icon_trash_frame.pack(side="left", padx=(0, 14))
icon_trash_frame.pack_propagate(False)
ctk.CTkLabel(icon_trash_frame, text="🗑️", font=("Segoe UI", 14),
             fg_color="transparent", text_color="#F87171").place(relx=0.8, rely=0.5, anchor="center")

tf1 = ctk.CTkFrame(row1, fg_color="transparent")
tf1.pack(side="left")
ctk.CTkLabel(tf1, text="Permanent Delete",  font=("Segoe UI", 13, "bold"), text_color="#FFFFFF",  height=18).pack(anchor="w")
ctk.CTkLabel(tf1, text="Skip Recycle Bin",  font=("Segoe UI", 11),         text_color="#9CA3AF",  height=16).pack(anchor="w")
switch_hard = ctk.CTkSwitch(row1, text="", width=40, progress_color="#4F46E5")
switch_hard.pack(side="right")

row2 = ctk.CTkFrame(container_left, fg_color="transparent")
row2.pack(fill="x", pady=(0, 14))

icon_bell_frame = ctk.CTkFrame(row2, width=36, height=36, corner_radius=8, fg_color="#1E2B4D")
icon_bell_frame.pack(side="left", padx=(0, 14))
icon_bell_frame.pack_propagate(False)
ctk.CTkLabel(icon_bell_frame, text="🔔", font=("Segoe UI", 14),
             fg_color="transparent", text_color="#60A5FA").place(relx=0.5, rely=0.5, anchor="center")

tf2 = ctk.CTkFrame(row2, fg_color="transparent")
tf2.pack(side="left")
ctk.CTkLabel(tf2, text="OS Notifications",     font=("Segoe UI", 13, "bold"), text_color="#FFFFFF",  height=18).pack(anchor="w")
ctk.CTkLabel(tf2, text="Enable system alerts", font=("Segoe UI", 11),         text_color="#9CA3AF",  height=16).pack(anchor="w")
switch_notify = ctk.CTkSwitch(row2, text="", width=40, progress_color="#4F46E5")
switch_notify.pack(side="right")

row3 = ctk.CTkFrame(container_left, fg_color="transparent")
row3.pack(fill="x", pady=(0, 5))
ctk.CTkLabel(row3, text=">_",              font=("Consolas", 14, "bold"), text_color="#9CA3AF").pack(side="left", padx=(0, 8))
ctk.CTkLabel(row3, text="Trigger Command", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF").pack(side="left")

entry_cmd = ctk.CTkEntry(container_left, fg_color="#15161A", border_color="#374151",
                          text_color="#FFFFFF", font=("Segoe UI", 13), height=34)
entry_cmd.pack(fill="x", pady=(0, 12))

row4 = ctk.CTkFrame(container_left, fg_color="transparent")
row4.pack(fill="x", pady=(0, 5))
ctk.CTkLabel(row4, text="〰",               font=("Segoe UI", 14, "bold"), text_color="#9CA3AF").pack(side="left", padx=(0, 8))
ctk.CTkLabel(row4, text="Scan Interval (s)", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF").pack(side="left")

entry_interval = ctk.CTkEntry(container_left, fg_color="#15161A", border_color="#374151",
                               text_color="#FFFFFF", font=("Segoe UI", 13), height=34)
entry_interval.pack(fill="x", pady=(0, 12))

row5 = ctk.CTkFrame(container_left, fg_color="transparent")
row5.pack(fill="x", pady=(0, 5))
ctk.CTkLabel(row5, text="+  ",                   font=("Consolas", 14, "bold"), text_color="#9CA3AF").pack(side="left", padx=(0, 8))
ctk.CTkLabel(row5, text="Extend Presets (min)",  font=("Segoe UI", 13, "bold"), text_color="#FFFFFF").pack(side="left")
ctk.CTkLabel(row5, text="comma-separated", font=("Segoe UI", 10), text_color="#6B7280").pack(side="right")

entry_presets = ctk.CTkEntry(container_left, fg_color="#15161A", border_color="#374151",
                              text_color="#FFFFFF", font=("Segoe UI", 13), height=34,
                              placeholder_text="e.g. 5,15,30,60")
entry_presets.pack(fill="x")



col_right = ctk.CTkFrame(main_area, fg_color="transparent")
col_right.pack(side="right", fill="both", expand=True, padx=(28, 0))

header_right = ctk.CTkFrame(col_right, fg_color="transparent")
header_right.pack(fill="x", pady=(0, 12))
ctk.CTkLabel(header_right, text="🕒", font=("Segoe UI", 16), text_color="#60A5FA").pack(side="left", padx=(0, 8))
ctk.CTkLabel(header_right, text="Active Countdown List", font=("Segoe UI", 15, "bold"), text_color="#FFFFFF").pack(side="left")
badge_items = ctk.CTkLabel(header_right, text="0 Items",
                            font=("Segoe UI", 11, "bold"), fg_color="#18181A",
                            text_color="#60A5FA", corner_radius=10, width=60, height=24)
badge_items.pack(side="right")

card_right = ctk.CTkFrame(col_right, fg_color="#1F2025", corner_radius=12)
card_right.pack(fill="both", expand=True)

list_header = ctk.CTkFrame(card_right, fg_color="transparent", height=36)
list_header.pack(fill="x", padx=18, pady=(12, 0))
ctk.CTkLabel(list_header, text="FILE NAME",     font=("Segoe UI", 11, "bold"), text_color="#9CA3AF").pack(side="left")
ctk.CTkLabel(list_header, text="ACTIONS / TIME", font=("Segoe UI", 11, "bold"), text_color="#9CA3AF").pack(side="right")

ctk.CTkFrame(card_right, fg_color="#374151", height=1).pack(fill="x", padx=10, pady=(4, 0))

frame_empty = ctk.CTkFrame(card_right, fg_color="transparent")
ctk.CTkLabel(frame_empty, text="📭", font=("Segoe UI", 48)).pack(pady=(50, 10))
ctk.CTkLabel(frame_empty, text="No active countdowns",
             font=("Segoe UI", 14, "bold"), text_color="#FFFFFF").pack(pady=(0, 5))
ctk.CTkLabel(frame_empty, text="Files waiting for deletion will appear here.",
             font=("Segoe UI", 12), text_color="#9CA3AF").pack()

scroll_list = ctk.CTkScrollableFrame(card_right, fg_color="transparent", bg_color="transparent")



def show_extend_dialog(fname):
    presets = parse_extend_presets()
    if not presets:
        return

    cols   = min(3, len(presets))
    n_rows = (len(presets) + cols - 1) // cols
    dlg_h  = 90 + n_rows * 46

    dlg = ctk.CTkToplevel(app)
    dlg.title("Extend Timer")
    dlg.geometry(f"224x{dlg_h}+{app.winfo_x() + 340}+{app.winfo_y() + 190}")
    dlg.resizable(False, False)
    dlg.configure(fg_color="#1F2025")
    dlg.attributes("-topmost", True)
    dlg.grab_set()

    short = fname if len(fname) <= 22 else fname[:19] + "..."
    ctk.CTkLabel(dlg, text=f"Extend: {short}",
                 font=("Segoe UI", 11), text_color="#9CA3AF").pack(pady=(12, 8), padx=14)

    grid_frame = ctk.CTkFrame(dlg, fg_color="transparent")
    grid_frame.pack(padx=14, fill="x")

    for i, minutes in enumerate(presets):
        col_i = i % cols
        row_i = i // cols
        grid_frame.columnconfigure(col_i, weight=1)
        btn = ctk.CTkButton(grid_frame, text=_ext_label(minutes),
                             font=("Segoe UI", 12, "bold"),
                             height=34, fg_color="#4F46E5", hover_color="#4338CA",
                             corner_radius=8)
        btn.configure(command=lambda m=minutes: [extend_timer(fname, m), dlg.destroy()])
        btn.grid(row=row_i, column=col_i, padx=3, pady=3, sticky="ew")

    ctk.CTkButton(dlg, text="Close",
                  fg_color="#374151", hover_color="#4B5563",
                  height=26, corner_radius=8,
                  command=dlg.destroy).pack(pady=(8, 10), padx=14, fill="x")



def update_ui_state():
    if is_active:
        btn_toggle.configure(text="⏹ Stop System",  fg_color="#C8102E", hover_color="#990C23")
    else:
        btn_toggle.configure(text="▶ Start System", fg_color="#10B981", hover_color="#059669")


def toggle_state():
    global is_active
    is_active = not is_active
    log(f"SYSTEM {'STARTED' if is_active else 'STOPPED'}")
    update_ui_state()


btn_toggle.configure(command=toggle_state)



def save_settings_from_ui(*args):
    config["hard_delete"]   = bool(switch_hard.get())
    config["notifications"] = bool(switch_notify.get())

    cmd_val = entry_cmd.get().strip()
    if cmd_val:
        config["command"] = cmd_val

    try:
        config["scan_interval"] = max(1, int(entry_interval.get().strip()))
    except ValueError:
        pass

    presets_raw = entry_presets.get().strip()
    presets     = [int(x.strip()) for x in presets_raw.split(",") if x.strip().isdigit()]
    if presets:
        config["extend_presets"] = presets

    save_data()


switch_hard.configure(command=save_settings_from_ui)
switch_notify.configure(command=save_settings_from_ui)
for _w in (entry_cmd, entry_interval, entry_presets):
    _w.bind("<FocusOut>", save_settings_from_ui)
    _w.bind("<Return>",   save_settings_from_ui)


def init_ui_values():
    if config.get("hard_delete"):   switch_hard.select()
    if config.get("notifications"): switch_notify.select()
    entry_cmd.insert(0, config.get("command", "del"))
    entry_interval.insert(0, str(config.get("scan_interval", 5)))
    presets = config.get("extend_presets", [5, 15, 30])
    entry_presets.insert(0, ",".join(str(p) for p in presets))
    update_ui_state()


def refresh_ui_list():
    if not running:
        return

    try:
        with lock:
            current    = dict(scheduled)
            cur_paused = dict(paused)

        count = len(current)
        badge_items.configure(text=f"{count} Items")

        if count == 0:
            scroll_list.pack_forget()
            frame_empty.pack(expand=True, fill="both")
        else:
            frame_empty.pack_forget()
            scroll_list.pack(expand=True, fill="both", padx=8, pady=6)

            for fname, dt in current.items():
                is_paused = fname in cur_paused

                if is_paused:
                    rem     = "⏸ " + _fmt_secs(cur_paused[fname])
                    t_color = "#F59E0B"
                elif not is_active:
                    rem     = "— paused —"
                    t_color = "#6B7280"
                else:
                    rem     = _remaining_str(dt)
                    t_color = _time_color(dt)

                pause_icon = "▶" if is_paused else "⏸"
                pause_fg   = "#10B981" if is_paused else "#374151"
                pause_hov  = "#059669" if is_paused else "#4B5563"

                if fname in list_widgets:
                    w = list_widgets[fname]
                    w["time_lbl"].configure(text=rem, text_color=t_color)
                    w["pause_btn"].configure(text=pause_icon,
                                             fg_color=pause_fg,
                                             hover_color=pause_hov)
                else:
                    row = ctk.CTkFrame(scroll_list, fg_color="#2A2C35", corner_radius=8)
                    row.pack(fill="x", pady=3, padx=3)

                    btn_x = ctk.CTkButton(
                        row, text="✕", width=28, height=28,
                        font=("Segoe UI", 11, "bold"),
                        fg_color="#7F1D1D", hover_color="#991B1B",
                        corner_radius=6,
                        command=lambda f=fname: cancel_timer(f)
                    )
                    btn_x.pack(side="right", padx=(0, 8), pady=5)

                    t_lbl = ctk.CTkLabel(
                        row, text=rem, font=("Segoe UI", 12),
                        text_color=t_color, width=96, anchor="e"
                    )
                    t_lbl.pack(side="right", padx=(0, 4), pady=5)

                    btn_ext = ctk.CTkButton(
                        row, text="+", width=28, height=28,
                        font=("Segoe UI", 15, "bold"),
                        fg_color="#1E3A5F", hover_color="#2563EB",
                        corner_radius=6,
                        command=lambda f=fname: show_extend_dialog(f)
                    )
                    btn_ext.pack(side="right", padx=(0, 4), pady=5)

                    btn_p = ctk.CTkButton(
                        row, text=pause_icon, width=28, height=28,
                        font=("Segoe UI", 12),
                        fg_color=pause_fg, hover_color=pause_hov,
                        corner_radius=6,
                        command=lambda f=fname: toggle_pause_timer(f)
                    )
                    btn_p.pack(side="right", padx=(0, 4), pady=5)

                    short = fname if len(fname) <= 30 else fname[:27] + "…"
                    ctk.CTkLabel(
                        row, text=short, font=("Segoe UI", 12),
                        text_color="#FFFFFF", anchor="w"
                    ).pack(side="left", padx=(10, 0), pady=5, fill="x", expand=True)

                    list_widgets[fname] = {
                        "frame":     row,
                        "time_lbl":  t_lbl,
                        "pause_btn": btn_p,
                    }

            for f in list(list_widgets.keys()):
                if f not in current:
                    list_widgets[f]["frame"].destroy()
                    del list_widgets[f]

    except Exception as e:
        log(f"UI REFRESH ERROR: {e}")

    app.after(1000, refresh_ui_list)


def quit_app(icon=None, item=None):
    global running
    running = False
    if icon:
        try:
            icon.stop()
        except Exception:
            pass
    app.quit()
    log("EXIT")


if (ctypes.windll.kernel32.CreateMutexW(None, False, "TxtTimer_SingleInstance")
        and ctypes.windll.kernel32.GetLastError() == 183):
    ctypes.windll.user32.MessageBoxW(0, "TXT Timer is already running.", "TXT Timer", 0x30)
    sys.exit(0)



DATA_DIR.mkdir(parents=True, exist_ok=True)
log("STARTED")
load_data()
init_ui_values()

threading.Thread(target=scanner_loop, daemon=True).start()
app.after(1000, refresh_ui_list)


def setup_tray():
    try:
        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard",
                             lambda: app.after(0, app.deiconify), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", quit_app),
        )
        icon = pystray.Icon("TxtTimer", get_icon_image(), "TXT Timer", menu)
        icon.run()
    except Exception as e:
        log(f"TRAY ERROR: {e}")


threading.Thread(target=setup_tray, daemon=True).start()
app.mainloop()
