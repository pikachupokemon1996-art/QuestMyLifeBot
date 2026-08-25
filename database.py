import json, os, tempfile, uuid
from pathlib import Path

DB = Path(__file__).resolve().parent / "users.json"

def default_user(name):
    return {
        "name": name or "Герой",
        "level": 1,
        "xp": 0,
        "quests_completed": 0,
        "achievements": [],
        "inventory": [],
        "stats": {"courage": 1, "wisdom": 1, "discipline": 1},
        "active_quest": None,
    }

def normalize(u, name=None):
    u.setdefault("name", name or "Герой")
    if name:
        u["name"] = name
    u.setdefault("xp", 0)
    u.setdefault("level", max(1, int(u["xp"]) // 100 + 1))
    u.setdefault("quests_completed", 0)
    u.setdefault("achievements", [])
    u.setdefault("inventory", [])
    u.setdefault("stats", {"courage": 1, "wisdom": 1, "discipline": 1})
    u.setdefault("active_quest", None)
    u["level"] = max(int(u.get("level", 1)), int(u["xp"]) // 100 + 1)
    return u

def load():
    if not DB.exists():
        return {}
    try:
        data = json.loads(DB.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save(users):
    fd, tmp = tempfile.mkstemp(prefix="users_", suffix=".json", dir=str(DB.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def create_or_update_user(user_id, name):
    users = load()
    key = str(user_id)
    users[key] = normalize(users.get(key, default_user(name)), name)
    save(users)
    return users[key]

def get_user(user_id):
    users = load()
    u = users.get(str(user_id))
    return normalize(u) if u else None

def set_active_quest(user_id, task, text, reward=25):
    users = load()
    key = str(user_id)
    u = normalize(users.get(key, default_user("Герой")))
    quest = {
        "id": uuid.uuid4().hex[:12],
        "task": task,
        "text": text,
        "reward": int(reward),
        "completed": False,
    }
    u["active_quest"] = quest
    users[key] = u
    save(users)
    return quest

def get_active_quest(user_id):
    u = get_user(user_id)
    return u.get("active_quest") if u else None

def update_active_quest_text(user_id, quest_id, text):
    users = load()
    key = str(user_id)
    u = users.get(key)
    if not u:
        return False
    u = normalize(u)
    q = u.get("active_quest")
    if not q or q.get("id") != quest_id or q.get("completed"):
        return False
    q["text"] = text
    u["active_quest"] = q
    users[key] = u
    save(users)
    return True

def complete_active_quest(user_id, quest_id):
    users = load()
    key = str(user_id)
    u = users.get(key)
    if not u:
        return {"status": "no_user"}
    u = normalize(u)
    q = u.get("active_quest")
    if not q or q.get("id") != quest_id:
        return {"status": "stale"}
    if q.get("completed"):
        return {"status": "already_completed", "user": u}

    old_level = int(u["level"])
    reward = int(q.get("reward", 25))
    q["completed"] = True
    u["active_quest"] = q
    u["xp"] = int(u.get("xp", 0)) + reward
    u["quests_completed"] = int(u.get("quests_completed", 0)) + 1
    u["level"] = max(1, u["xp"] // 100 + 1)

    a = u.setdefault("achievements", [])
    if u["quests_completed"] == 1 and "Первый квест" not in a:
        a.append("Первый квест")
    level_up = u["level"] > old_level
    if level_up:
        item = f"Уровень {u['level']}"
        if item not in a:
            a.append(item)

    users[key] = u
    save(users)
    return {"status": "completed", "reward": reward, "level_up": level_up, "user": u}
