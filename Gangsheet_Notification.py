import os
import re
import time
import threading
import requests
import dropbox

# ==== CONFIG (lấy từ ENV, có fallback an toàn) ====
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()     # để trống nếu chưa chắc
DROPBOX_TOKEN_API = os.getenv("DROPBOX_TOKEN_API", "").strip()
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER", "/gangsheet/").strip()
SCHEDULE_INTERVAL = int(os.getenv("SCHEDULE_INTERVAL", "1800"))  # 1800s = 30 phút
OFFSET = None
BOT_USERNAME = None

# ép kiểu CHAT_ID nếu có
if CHAT_ID.isdigit() or (CHAT_ID.startswith("-") and CHAT_ID[1:].isdigit()):
    CHAT_ID = int(CHAT_ID)
else:
    CHAT_ID = None  # sẽ add sau khi bot nhận tin nhắn

# kiểm tra cấu hình tối thiểu
if not TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN (đặt trong App Spec envs).")
if not DROPBOX_TOKEN_API:
    raise RuntimeError("Thiếu DROPBOX_TOKEN_API (đặt trong App Spec envs).")

# Danh sách các chat đã đăng ký nhận báo cáo định kỳ (set id)
SUBSCRIBERS = set()
_subs_lock = threading.Lock()

def build_help():
    uname = BOT_USERNAME or "Bot"
    return (
        "Danh sách lệnh:\n"
        f"@{uname} gang - Hiển thị danh sách file .tif.\n"
        f"@{uname} info - Hiển thị danh sách lệnh.\n"
        f"Gõ @{uname} một mình để nhận gợi ý."
    )

def send_message(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    params = {"chat_id": chat_id, "text": text}
    try:
        requests.get(url, params=params, timeout=20)
    except Exception as e:
        print(f"Lỗi send_message: {e}")

def check_dropbox():
    # Lấy token Dropbox
    resp = requests.get(DROPBOX_TOKEN_API)
    if resp.status_code != 200:
        return f"Lỗi lấy token Dropbox: {resp.status_code}"
    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return "Không tìm thấy access_token trong phản hồi API."

    # Kết nối Dropbox
    dbx = dropbox.Dropbox(access_token, timeout=900)

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
        entries = list(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)

        tif_files = [
            e.name for e in entries
            if type(e).__name__ == "FileMetadata" and e.name.lower().endswith(".tif")
        ]

        def sort_key(fname: str):
            parts = fname.split('_')
            if len(parts) > 1:
                try:
                    return int(parts[1])
                except ValueError:
                    return float('inf')
            return float('inf')

        tif_files.sort(key=sort_key)
        if not tif_files:
            return "Không có file .tif nào."

        # Tìm End id lớn nhất: lấy phần thứ 3 (index 2) dạng 'start-end', lấy số sau dấu '-'
        end_ids = []
        for fname in tif_files:
            parts = fname.split('_')
            if len(parts) > 2 and '-' in parts[2]:
                range_part = parts[2]
                tail = range_part.split('-')[-1]
                # tail có thể chứa ký tự khác nếu format thay đổi; chỉ lấy số đầu tiên
                num = ''
                for ch in tail:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                if num:
                    try:
                        end_ids.append(int(num))
                    except ValueError:
                        pass
        end_id_line = f"End id: {max(end_ids)}" if end_ids else "End id: -"

        header = f"Số lượng: {len(tif_files)}\n{end_id_line}"
        body = "\n".join(tif_files)
        return f"{header}\n{body}"
    except Exception as e:
        return f"Lỗi khi liệt kê Dropbox: {e}"

def scheduler_loop():
    while True:
        try:
            result_msg = check_dropbox()
            with _subs_lock:
                targets = list(SUBSCRIBERS) or [CHAT_ID]
            for cid in targets:
                send_message(result_msg, cid)
        except Exception as e:
            print(f"Lỗi scheduler: {e}")
        time.sleep(SCHEDULE_INTERVAL)

def main():
    global OFFSET, BOT_USERNAME
    print("Bot đang chạy... (gõ /gangsheet tif hoặc @BotUsername gang; tự động gửi mỗi 30 phút)")

    # Lấy bot username (getMe)
    try:
        me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=15).json()
        BOT_USERNAME = me.get("result", {}).get("username", "")
        print(f"Bot username: {BOT_USERNAME}")
    except Exception as e:
        print(f"Không lấy được username bot: {e}")

    # Đăng ký chat mặc định ban đầu
    with _subs_lock:
        SUBSCRIBERS.add(int(CHAT_ID))

    # chạy thread scheduler
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # gửi ngay lần đầu tới subscribers
    # try:
    #     initial = check_dropbox()
    #     with _subs_lock:
    #         for cid in SUBSCRIBERS:
    #             send_message(initial, cid)
    # except Exception as e:
    #     print(f"Lỗi gửi lần đầu: {e}")

    # Polling loop
    while True:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        params = {"timeout": 30, "offset": OFFSET}
        try:
            res = requests.get(url, params=params, timeout=35).json()
        except Exception as e:
            print(f"Lỗi khi getUpdates: {e}")
            continue

        for update in res.get("result", []):
            OFFSET = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = message["chat"]["id"]
            text_raw = message.get("text", "") or ""
            entities = message.get("entities", [])

            # Chuẩn hóa chuỗi
            cleaned = text_raw.strip()
            low = cleaned.lower()

            # Nếu có entities type mention đầu chuỗi, bóc tách
            if entities:
                for ent in entities:
                    if ent.get("type") == "mention" and ent.get("offset") == 0:
                        length = ent.get("length", 0)
                        mention = cleaned[:length]
                        if BOT_USERNAME and mention.lower() == f"@{BOT_USERNAME.lower()}":
                            cleaned = cleaned[length:].strip()
                            low = cleaned.lower()
                        break

            # Bỏ mention nội tuyến còn sót
            if BOT_USERNAME:
                low = re.sub(rf"@{re.escape(BOT_USERNAME.lower())}\b", "", low).strip()

            # Tách tokens để xử lý mention phức tạp
            tokens = [t for t in re.split(r"\s+", text_raw.strip()) if t]
            mentioned = False
            if BOT_USERNAME:
                bot_lower = BOT_USERNAME.lower()
                for tok in tokens:
                    if tok.lower() == f"@{bot_lower}":
                        mentioned = True
                        break

            # Mention đứng một mình -> gợi ý
            if mentioned and len(tokens) == 1:
                send_message(f"Gì? Gõ @{BOT_USERNAME} info để xem lệnh.", chat_id)
                with _subs_lock:
                    SUBSCRIBERS.add(int(chat_id))
                continue

            # Lệnh info
            if mentioned and len(tokens) >= 2 and tokens[1].lower() == 'info':
                send_message(build_help(), chat_id)
                with _subs_lock:
                    SUBSCRIBERS.add(int(chat_id))
                continue

            trigger = False
            if low in {"/gangsheet tif", "gangsheet tif"}:
                trigger = True
            elif BOT_USERNAME and low.startswith(f"/gangsheet@{BOT_USERNAME.lower()}") and low.endswith(" tif"):
                trigger = True
            elif mentioned and len(tokens) >= 2 and tokens[1].lower() == 'gang':
                trigger = True

            if trigger:
                print("Nhận lệnh gangsheet tif -> chạy check_dropbox() từ chat", chat_id)
                with _subs_lock:
                    SUBSCRIBERS.add(int(chat_id))
                result_msg = check_dropbox()
                send_message(result_msg, chat_id)
                continue

            # Mention nhưng không hợp lệ
            if mentioned:
                send_message(f"Có cái nịt. Gõ @{BOT_USERNAME} info để xem lệnh.", chat_id)
                with _subs_lock:
                    SUBSCRIBERS.add(int(chat_id))

if __name__ == "__main__":
    main()
