import asyncio
import datetime as dt
import json
import os
import platform
import re
import secrets
import string
import sys
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


PREFIX = "s!"
KST = dt.timezone(dt.timedelta(hours=9))
CONFIG_DIR = Path("opal_guild_configs")
CONFIG_DIR.mkdir(exist_ok=True)

BLUE = 0x5865F2
GREEN = 0x2ECC71
YELLOW = 0xF1C40F
RED = 0xD64F5F
DARK = 0x2B2D31
ORANGE = 0xF0A45D
CARD = 0x34443E
TIMEOUT = 0x9B8CFF
UNTIMEOUT = 0x7CF0C6
KICK = 0xFF9A86
HELP = 0x6EA8FE
HELP_WARN = 0xF0A45D
HELP_PUNISH = 0x9B8CFF
HELP_SETTING = 0x7CF0C6
PUNISHMENT_ACTIONS = {"warn", "timeout", "untimeout", "kick", "ban", "unban", "purge"}
PUNISHMENT_LABELS = {
    "warn": "경고",
    "timeout": "타임아웃",
    "untimeout": "타임아웃 해제",
    "kick": "추방",
    "ban": "차단",
    "unban": "차단 해제",
    "purge": "청소",
}
DEFAULT_EMBED_EMOJIS = {
    "timeout": "🔮",
    "untimeout": "🟩",
    "kick": "📤",
    "ban": "🚨",
    "unban": "🔓",
    "warn": "🚨",
    "warn_delete": "🧹",
    "warn_clear": "♻️",
    "edit": "📝",
}
SUCCESS_EMOJI = "<a:ok:1514622289621028894>"
FAIL_EMOJI = "<a:no:1514622340229496972>"
FAIL_TITLE_KEYWORDS = ("실패", "오류", "부족", "불가", "초과", "없음")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
STARTED_AT = dt.datetime.now(dt.timezone.utc)
TIMEOUT_RELEASE_TASKS: dict[tuple[int, int], asyncio.Task] = {}
PENDING_MANUAL_UNMUTES: set[tuple[int, int]] = set()
SCHEDULED_TIMEOUTS_ON_READY = False


DEFAULT_CONFIG = {
    "log_channel": None,
    "warnings": {},
    "punishments": {},
    "active_timeouts": {},
    "warning_auto_rules": [],
    "case_count": 0,
    "default_timeout_seconds": 600,
    "staff_roles": [],
    "role_timeout_limits": {},
    "role_punishment_limits": {},
    "embed_emojis": DEFAULT_EMBED_EMOJIS,
}


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def config_path(guild_id: int) -> Path:
    return CONFIG_DIR / f"{guild_id}.json"


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_config(raw: dict | None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(raw, dict):
        config.update(raw)

    if not isinstance(config.get("warnings"), dict):
        config["warnings"] = {}
    if not isinstance(config.get("punishments"), dict):
        config["punishments"] = {}
    if not isinstance(config.get("active_timeouts"), dict):
        config["active_timeouts"] = {}
    if not isinstance(config.get("warning_auto_rules"), list):
        config["warning_auto_rules"] = []
    normalized_rules = []
    for rule in config["warning_auto_rules"]:
        if not isinstance(rule, dict):
            continue
        threshold = rule.get("threshold")
        action = rule.get("action")
        if not str(threshold).isdigit() or action not in ("timeout", "kick", "ban"):
            continue
        normalized_rules.append({
            "threshold": int(threshold),
            "action": action,
            "duration_seconds": int(rule.get("duration_seconds", 600)) if str(rule.get("duration_seconds", 600)).isdigit() else 600,
        })
    config["warning_auto_rules"] = normalized_rules
    if not isinstance(config.get("case_count"), int):
        config["case_count"] = 0
    if not isinstance(config.get("default_timeout_seconds"), int):
        config["default_timeout_seconds"] = 600
    if config["default_timeout_seconds"] < 1:
        config["default_timeout_seconds"] = 600
    if not isinstance(config.get("staff_roles"), list):
        config["staff_roles"] = []
    config["staff_roles"] = sorted({int(role_id) for role_id in config["staff_roles"] if str(role_id).isdigit()})
    if not isinstance(config.get("role_timeout_limits"), dict):
        config["role_timeout_limits"] = {}
    config["role_timeout_limits"] = {
        str(role_id): int(seconds)
        for role_id, seconds in config["role_timeout_limits"].items()
        if str(role_id).isdigit() and str(seconds).isdigit() and int(seconds) > 0
    }
    if not isinstance(config.get("role_punishment_limits"), dict):
        config["role_punishment_limits"] = {}
    normalized_limits = {}
    for role_id, value in config["role_punishment_limits"].items():
        if not str(role_id).isdigit() or not isinstance(value, dict):
            continue
        actions = value.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        allowed_actions = sorted({action for action in actions if action in PUNISHMENT_ACTIONS})
        limit = value.get("timeout_seconds")
        timeout_seconds = None
        if str(limit).isdigit() and int(limit) > 0:
            timeout_seconds = int(limit)
        normalized_limits[str(role_id)] = {
            "actions": allowed_actions,
            "timeout_seconds": timeout_seconds,
        }
    for role_id, seconds in config["role_timeout_limits"].items():
        normalized_limits.setdefault(str(role_id), {"actions": sorted(PUNISHMENT_ACTIONS), "timeout_seconds": None})
        normalized_limits[str(role_id)]["timeout_seconds"] = int(seconds)
        if "timeout" not in normalized_limits[str(role_id)]["actions"]:
            normalized_limits[str(role_id)]["actions"].append("timeout")
            normalized_limits[str(role_id)]["actions"].sort()
    config["role_punishment_limits"] = normalized_limits
    if not isinstance(config.get("embed_emojis"), dict):
        config["embed_emojis"] = {}
    config["embed_emojis"] = {
        key: str(config["embed_emojis"].get(key) or value).strip()[:40] or value
        for key, value in DEFAULT_EMBED_EMOJIS.items()
    }
    if config.get("log_channel") is not None:
        try:
            config["log_channel"] = int(config["log_channel"])
        except (TypeError, ValueError):
            config["log_channel"] = None
    return config


def load_config(guild_id: int) -> dict:
    path = config_path(guild_id)
    if not path.exists():
        config = normalize_config(None)
        save_config(guild_id, config)
        return config

    try:
        return normalize_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"설정 로드 실패: {path} - {exc}")
        return normalize_config(None)


def save_config(guild_id: int, config: dict) -> None:
    path = config_path(guild_id)
    data = json.dumps(normalize_config(config), ensure_ascii=False, indent=4)
    last_error = None

    for attempt in range(6):
        tmp_path = path.with_name(f"{path.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            tmp_path.write_text(data, encoding="utf-8")
            os.replace(tmp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))

    try:
        path.write_text(data, encoding="utf-8")
    except OSError as exc:
        raise last_error or exc


def next_case_id(config: dict) -> int:
    config["case_count"] = int(config.get("case_count", 0)) + 1
    return config["case_count"]


def make_case_code(config: dict, length: int = 7) -> str:
    alphabet = string.ascii_letters + string.digits
    used = {
        str(record.get("case_code", "")).lower()
        for record in config.get("punishments", {}).values()
        if isinstance(record, dict)
    }
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if code.lower() not in used:
            return code


def status_title(title: str, color: int) -> str:
    if title.startswith((SUCCESS_EMOJI, FAIL_EMOJI)):
        return title
    if color == GREEN:
        return f"{SUCCESS_EMOJI} {title}"
    if color == RED or any(keyword in title for keyword in FAIL_TITLE_KEYWORDS):
        return f"{FAIL_EMOJI} {title}"
    return title


def make_embed(title: str, description: str | None = None, color: int = BLUE) -> discord.Embed:
    title = status_title(title, color)
    embed = discord.Embed(title=title, description=description, color=color, timestamp=now_kst())
    embed.set_footer(text="Opal")
    return embed


def short_reason(reason: str | None) -> str:
    return (reason or "운영진 재량").strip()[:900]


def code_block_text(value: str | None) -> str:
    return short_reason(value).replace("```", "`\u200b``")


def seconds_to_duration_text(seconds: int) -> str:
    return seconds_to_detail_duration_text(seconds)


def seconds_to_detail_duration_text(seconds: int) -> str:
    seconds = max(1, int(seconds))
    units = (
        ("일", 86400),
        ("시간", 3600),
        ("분", 60),
        ("초", 1),
    )
    parts = []
    for label, unit_seconds in units:
        amount, seconds = divmod(seconds, unit_seconds)
        if amount:
            parts.append(f"{amount}{label}")
    return " ".join(parts) if parts else "1초"


def timeout_until_duration_text(until: dt.datetime, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    seconds = max(1, round((until.astimezone(dt.timezone.utc) - now).total_seconds()))
    return seconds_to_detail_duration_text(seconds)


def uptime_text() -> str:
    seconds = int((dt.datetime.now(dt.timezone.utc) - STARTED_AT).total_seconds())
    return seconds_to_detail_duration_text(seconds)


async def is_developer(user: discord.abc.User) -> bool:
    developer_id = os.getenv("DEVELOPER_ID") or os.getenv("BOT_DEVELOPER_ID")
    if developer_id and developer_id.isdigit() and int(developer_id) == user.id:
        return True
    try:
        return await bot.is_owner(user)
    except discord.DiscordException:
        return False


def member_text(user: discord.abc.User) -> str:
    mention = getattr(user, "mention", str(user))
    return f"{mention} (`{user.id}`)"


def display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", str(user))


def case_id_text(case_id: int | str) -> str:
    return str(case_id).lstrip("#")


def action_name(action: str) -> str:
    if "뮤트" in action:
        return "타임아웃 처벌"
    if action == "언뮤트":
        return "타임아웃 해제"
    if "경고" in action:
        return "경고 처벌"
    if action == "킥":
        return "추방 처벌"
    if action == "밴":
        return "차단 처벌"
    if action == "언밴":
        return "차단 해제"
    return action


def action_duration(action: str) -> str:
    match = re.search(r"\(([^)]+)\)", action)
    if not match:
        return "-"
    raw = match.group(1).replace(" - 기본 시간", "").replace(" ", "")
    parsed = parse_duration(raw)
    return seconds_to_duration_text(int(parsed.total_seconds())) if parsed else raw


def stacked_timeout_until(member: discord.Member, delta: dt.timedelta) -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    current_until = member.timed_out_until
    if current_until and current_until.astimezone(dt.timezone.utc) > now:
        return current_until.astimezone(dt.timezone.utc) + delta
    return now + delta


def guild_from_user(user: discord.abc.User) -> discord.Guild | None:
    return getattr(user, "guild", None)


def embed_emoji(guild: discord.Guild | None, key: str) -> str:
    if not guild:
        return DEFAULT_EMBED_EMOJIS[key]
    config = load_config(guild.id)
    return config.get("embed_emojis", {}).get(key, DEFAULT_EMBED_EMOJIS[key])


def footer_time() -> str:
    now = now_kst()
    ampm = "오전" if now.hour < 12 else "오후"
    hour = now.hour % 12 or 12
    return f"관리 · 오늘 {ampm} {hour}:{now.minute:02d}"


def user_avatar_url(user: discord.abc.User) -> str | None:
    avatar = getattr(user, "display_avatar", None)
    return avatar.url if avatar else None


def user_mention(user: discord.abc.User) -> str:
    return getattr(user, "mention", f"`{user.id}`")


def stored_user_mention(guild: discord.Guild, record: dict) -> str:
    target_id = record.get("target_id")
    if str(target_id).isdigit():
        member = guild.get_member(int(target_id))
        if member:
            return member.mention
        return f"<@{target_id}>"
    return f"`{record.get('target_name', target_id)}`"


def handler_text(moderator: discord.abc.User | None) -> str:
    return display_name(moderator) if moderator else "자동"


def warning_limit_for(guild: discord.Guild, total: int) -> int:
    config = load_config(guild.id)
    thresholds = sorted({
        int(rule.get("threshold"))
        for rule in config.get("warning_auto_rules", [])
        if str(rule.get("threshold")).isdigit()
    })
    if not thresholds:
        return 5
    return thresholds[-1]


def update_action_duration(action: str, duration_text: str) -> str:
    if "(" in action and ")" in action:
        return re.sub(r"\([^)]*\)", f"({duration_text})", action, count=1)
    if "뮤트" in action:
        return f"뮤트 ({duration_text})"
    return action


def recent_record_exists(config: dict, action: str, target_id: int, seconds: int = 10) -> bool:
    now = now_kst()
    for record in config.get("punishments", {}).values():
        if record.get("action") != action or int(record.get("target_id", 0)) != target_id:
            continue
        created_at = parse_time(record.get("created_at"))
        if created_at and abs((now - created_at.astimezone(KST)).total_seconds()) <= seconds:
            return True
    return False


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)
    except ValueError:
        return None


def save_punishment_record(
    config: dict,
    case_id: int,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None,
) -> None:
    case_code = make_case_code(config)
    config["punishments"][str(case_id)] = {
        "case_id": case_id,
        "case_code": case_code,
        "action": action,
        "target_id": target.id,
        "target_name": str(target),
        "moderator_id": moderator.id,
        "moderator_name": str(moderator),
        "reason": short_reason(reason),
        "created_at": now_kst().isoformat(),
        "updated_at": None,
    }


def get_case_code(config: dict, case_id: int | str) -> str:
    record = config.get("punishments", {}).get(str(case_id))
    if isinstance(record, dict) and record.get("case_code"):
        return str(record["case_code"])
    return str(case_id)


def find_punishment_record(config: dict, code_or_id: str) -> tuple[str | None, dict | None]:
    cleaned = code_or_id.strip().lstrip("#")
    if cleaned in config.get("punishments", {}):
        return cleaned, config["punishments"][cleaned]
    for key, record in config.get("punishments", {}).items():
        if not isinstance(record, dict):
            continue
        if str(record.get("case_code", "")).lower() == cleaned.lower():
            return key, record
    return None, None


def find_warning_record(
    config: dict,
    code_or_id: str,
    target_id: int | None = None,
) -> tuple[str | None, int | None, dict | None, str | None, dict | None]:
    cleaned = code_or_id.strip().lstrip("#")
    target_filter = str(target_id) if target_id else None

    for user_id, warnings in config.get("warnings", {}).items():
        if target_filter and str(user_id) != target_filter:
            continue
        if not isinstance(warnings, list):
            continue
        for index, warning in enumerate(warnings):
            if not isinstance(warning, dict):
                continue
            case_code = str(warning.get("case_code", ""))
            case_id = str(warning.get("case_id", ""))
            if case_id == cleaned or case_code.lower() == cleaned.lower():
                case_key, record = find_punishment_record(config, case_code or case_id)
                return str(user_id), index, warning, case_key, record

    case_key, record = find_punishment_record(config, cleaned)
    if not record or "경고" not in str(record.get("action", "")):
        return None, None, None, None, None
    user_id = str(record.get("target_id"))
    if target_filter and user_id != target_filter:
        return None, None, None, None, None
    for index, warning in enumerate(config.get("warnings", {}).get(user_id, [])):
        if str(warning.get("case_id")) == str(case_key) or str(warning.get("case_code", "")).lower() == str(record.get("case_code", "")).lower():
            return user_id, index, warning, case_key, record
    return None, None, None, case_key, record


def punishment_embed(
    title: str,
    case_id: int,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None,
    color: int,
) -> discord.Embed:
    guild = guild_from_user(target)
    emoji = embed_emoji(guild, "timeout" if "뮤트" in action else "unban" if action == "언밴" else "timeout")
    embed = discord.Embed(
        title=f"{emoji} {action_name(action)}",
        color=color or CARD,
        timestamp=now_kst(),
    )
    embed.set_author(name=case_id_text(case_id))
    duration = action_duration(action)
    embed.add_field(name="👥 대상자", value=user_mention(target), inline=True)
    if duration != "-":
        embed.add_field(name="🕒 지속시간", value=f"`{duration}`", inline=True)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    avatar_url = user_avatar_url(target)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def warning_embed(
    case_id: int,
    target: discord.Member,
    moderator: discord.abc.User,
    reason: str | None,
    total: int,
) -> discord.Embed:
    emoji = embed_emoji(target.guild, "warn")
    embed = discord.Embed(
        title=f"{emoji} 경고 지급",
        color=ORANGE,
        timestamp=now_kst(),
    )
    embed.set_author(name=case_id_text(case_id))
    embed.add_field(name="👥 대상자", value=target.mention, inline=True)
    embed.add_field(name="☑️ 경고누적", value=f"**{total} / {warning_limit_for(target.guild, total)}**", inline=True)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    embed.set_thumbnail(url=target.display_avatar.url)
    return embed


def warning_action_embed(
    guild: discord.Guild,
    action: str,
    target: discord.Member | discord.abc.User | None,
    moderator: discord.abc.User,
    reason: str | None,
    count_text: str,
    case_code: str | None = None,
) -> discord.Embed:
    emoji_key = "warn_clear" if action == "경고 초기화" else "warn_delete"
    emoji = embed_emoji(guild, emoji_key)
    embed = discord.Embed(
        title=f"{emoji} {action}",
        color=UNTIMEOUT,
        timestamp=now_kst(),
    )
    if case_code:
        embed.set_author(name=case_id_text(case_code))
    target_value = user_mention(target) if target else "`알 수 없음`"
    embed.add_field(name="👥 대상자", value=target_value, inline=True)
    embed.add_field(name="📌 처리", value=f"`{count_text}`", inline=True)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    avatar_url = user_avatar_url(target) if target else None
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def timeout_release_embed(
    target: discord.Member,
    moderator: discord.abc.User | None,
    reason: str,
    case_code: str | None = None,
) -> discord.Embed:
    emoji = embed_emoji(target.guild, "untimeout")
    embed = discord.Embed(
        title=f"{emoji} 타임아웃 해제",
        color=UNTIMEOUT,
        timestamp=now_kst(),
    )
    if case_code:
        embed.set_author(name=case_id_text(case_code))
    embed.add_field(name="👥 대상자", value=target.mention, inline=False)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    embed.set_thumbnail(url=target.display_avatar.url)
    return embed


def removal_embed(
    case_id: int,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None,
    color: int,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    guild = guild or guild_from_user(target)
    emoji_key = "ban" if action == "차단" else "kick"
    emoji = embed_emoji(guild, emoji_key)
    embed = discord.Embed(
        title=f"{emoji} {action} 처벌",
        color=color,
        timestamp=now_kst(),
    )
    embed.set_author(name=case_id_text(case_id))
    embed.add_field(name="👥 대상자", value=user_mention(target), inline=False)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    avatar_url = user_avatar_url(target)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def ban_action_embed(
    guild: discord.Guild,
    case_id: int | str,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User | None,
    reason: str | None,
) -> discord.Embed:
    is_unban = action in ("언밴", "차단 해제")
    emoji = embed_emoji(guild, "unban" if is_unban else "ban")
    title = "밴 해제" if is_unban else "차단 처벌"
    embed = discord.Embed(
        title=f"{emoji} {title}",
        color=UNTIMEOUT if is_unban else RED,
        timestamp=now_kst(),
    )
    embed.set_author(name=case_id_text(case_id))
    embed.add_field(name="👥 대상자", value=user_mention(target), inline=False)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    avatar_url = user_avatar_url(target)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


def edit_embed(
    guild: discord.Guild,
    code: str,
    record: dict,
    moderator: discord.abc.User,
    old_reason: str,
    duration_text: str | None = None,
    old_duration_text: str | None = None,
) -> discord.Embed:
    emoji = embed_emoji(guild, "edit")
    embed = discord.Embed(
        title=f"{emoji} 기록 수정",
        color=HELP_SETTING,
        timestamp=now_kst(),
    )
    embed.set_author(name=case_id_text(code))
    embed.add_field(name="👥 대상자", value=stored_user_mention(guild, record), inline=True)
    embed.add_field(name="처벌", value=f"`{action_name(str(record.get('action', '처벌')))}`", inline=True)
    if duration_text:
        embed.add_field(name="변경", value=f"지속시간 `{old_duration_text or '-'}` → `{duration_text}`", inline=False)
    embed.add_field(name="사유", value=f"```{code_block_text(record.get('reason'))}```", inline=False)
    if short_reason(old_reason) != short_reason(record.get("reason")):
        embed.add_field(name="이전", value=f"`{short_reason(old_reason)}`", inline=False)
    embed.set_footer(text=f"처리자: {handler_text(moderator)}")
    return embed


async def resolve_member(ctx: commands.Context, value: str) -> discord.Member | None:
    cleaned = value.strip().rstrip(",")
    mention_match = re.fullmatch(r"<@!?(\d+)>", cleaned)
    if mention_match:
        cleaned = mention_match.group(1)

    if cleaned.isdigit():
        member = ctx.guild.get_member(int(cleaned))
        if member:
            return member
        try:
            return await ctx.guild.fetch_member(int(cleaned))
        except discord.NotFound:
            return None
        except discord.DiscordException:
            return None

    converter = commands.MemberConverter()
    try:
        return await converter.convert(ctx, cleaned)
    except commands.BadArgument:
        return None


async def resolve_ban_target(ctx: commands.Context, value: str) -> discord.abc.User | discord.Object | None:
    member = await resolve_member(ctx, value)
    if member:
        return member

    cleaned = value.strip().rstrip(",")
    mention_match = re.fullmatch(r"<@!?(\d+)>", cleaned)
    if mention_match:
        cleaned = mention_match.group(1)

    if not cleaned.isdigit():
        return None

    user_id = int(cleaned)
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        return discord.Object(id=user_id)
    except discord.DiscordException:
        return discord.Object(id=user_id)


def can_target(ctx: commands.Context, target: discord.Member) -> tuple[bool, str | None]:
    if target == ctx.guild.owner:
        return False, "서버 소유자는 처벌할 수 없어요."
    if target == ctx.author:
        return False, "자기 자신은 처벌할 수 없어요."
    if target == ctx.guild.me:
        return False, "봇 자신은 처벌할 수 없어요."
    if ctx.author != ctx.guild.owner and target.top_role >= ctx.author.top_role:
        return False, "대상 유저의 역할이 명령어 사용자의 역할보다 같거나 높아요."
    if target.top_role >= ctx.guild.me.top_role:
        return False, "대상 유저의 역할이 봇 역할보다 같거나 높아요."
    return True, None


def parse_duration(value: str) -> dt.timedelta | None:
    if not value:
        return None

    cleaned = re.sub(r"\s+", "", value.lower().strip())
    cleaned = cleaned.replace("동안", "")
    aliases = {
        "하루": "1일",
        "일주일": "1주",
        "한주": "1주",
        "한시간": "1시간",
        "한분": "1분",
        "한초": "1초",
        "하룻": "1일",
    }
    cleaned = aliases.get(cleaned, cleaned)
    for suffix in ("이든", "이던", "인든", "든", "던", "이드", "디은", "이면", "까지"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    match = re.fullmatch(r"(\d+)(초|분|시간|일|주|s|m|h|d|w)", cleaned)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit in ("초", "s"):
        return dt.timedelta(seconds=amount)
    if unit in ("분", "m"):
        return dt.timedelta(minutes=amount)
    if unit in ("시간", "h"):
        return dt.timedelta(hours=amount)
    if unit in ("일", "d"):
        return dt.timedelta(days=amount)
    if unit in ("주", "w"):
        return dt.timedelta(weeks=amount)
    return None


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    config = load_config(member.guild.id)
    staff_roles = set(config.get("staff_roles", []))
    return any(role.id in staff_roles for role in member.roles)


def can_use_moderation(ctx: commands.Context) -> bool:
    return isinstance(ctx.author, discord.Member) and is_staff(ctx.author)


def max_timeout_for(member: discord.Member) -> int | None:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return None
    config = load_config(member.guild.id)
    punishment_limits = config.get("role_punishment_limits", {})
    configured_seconds = [
        int(limit["timeout_seconds"])
        for role in member.roles
        for limit in [punishment_limits.get(str(role.id))]
        if isinstance(limit, dict) and limit.get("timeout_seconds")
    ]
    if configured_seconds:
        return max(configured_seconds)

    limits = config.get("role_timeout_limits", {})
    seconds = [
        int(limits[str(role.id)])
        for role in member.roles
        if str(role.id) in limits
    ]
    return max(seconds) if seconds else None


def has_punishment_power(member: discord.Member, action: str) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    config = load_config(member.guild.id)
    staff_roles = set(config.get("staff_roles", []))
    member_staff_roles = [role for role in member.roles if role.id in staff_roles]
    if not member_staff_roles:
        return False

    limits = config.get("role_punishment_limits", {})
    configured = [
        limits.get(str(role.id))
        for role in member_staff_roles
        if str(role.id) in limits
    ]
    if not configured:
        return True
    return any(action in limit.get("actions", []) for limit in configured if isinstance(limit, dict))


async def require_punishment_power(ctx: commands.Context, action: str) -> bool:
    if isinstance(ctx.author, discord.Member) and has_punishment_power(ctx.author, action):
        return True
    label = PUNISHMENT_LABELS.get(action, action)
    await ctx.reply(embed=make_embed("처벌 권한 부족", f"현재 운영팀 역할은 `{label}` 처벌을 사용할 수 없어요.", RED), mention_author=False)
    return False


async def require_interaction_punishment_power(interaction: discord.Interaction, action: str) -> bool:
    if isinstance(interaction.user, discord.Member) and has_punishment_power(interaction.user, action):
        return True
    label = PUNISHMENT_LABELS.get(action, action)
    embed = make_embed("처벌 권한 부족", f"현재 운영팀 역할은 `{label}` 처벌을 사용할 수 없어요.", RED)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


class InteractionCommandContext:
    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.guild = interaction.guild
        self.author = interaction.user

    async def send(self, *args, **kwargs) -> discord.Message:
        kwargs.setdefault("ephemeral", True)
        return await self.interaction.followup.send(*args, **kwargs)


async def send_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    config = load_config(guild.id)
    channel_id = config.get("log_channel")
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            return await channel.send(embed=embed)
        except discord.DiscordException:
            pass
    return None


async def reply_to_log(guild: discord.Guild, timeout_data: dict | None, embed: discord.Embed) -> discord.Message | None:
    if not isinstance(timeout_data, dict):
        return await send_log(guild, embed)

    channel_id = timeout_data.get("log_channel_id")
    message_id = timeout_data.get("log_message_id")
    if not channel_id or not message_id:
        return await send_log(guild, embed)

    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return await send_log(guild, embed)

    try:
        message = await channel.fetch_message(int(message_id))
        return await message.reply(embed=embed, mention_author=False)
    except discord.DiscordException:
        return await send_log(guild, embed)


def attach_log_message(config: dict, case_id: int | str, message: discord.Message | None) -> None:
    if not message:
        return
    record = config.get("punishments", {}).get(str(case_id))
    if not isinstance(record, dict):
        return
    record["log_channel_id"] = message.channel.id
    record["log_message_id"] = message.id


def latest_timeout_record(config: dict, target_id: int) -> tuple[str | None, dict | None]:
    records = []
    for key, record in config.get("punishments", {}).items():
        if not isinstance(record, dict):
            continue
        if int(record.get("target_id", 0)) != target_id:
            continue
        if "뮤트" not in str(record.get("action", "")):
            continue
        records.append((int(record.get("case_id", 0)), key, record))
    if not records:
        return None, None
    _, key, record = max(records, key=lambda item: item[0])
    return key, record


def latest_ban_record(config: dict, target_id: int) -> tuple[str | None, dict | None]:
    records = []
    for key, record in config.get("punishments", {}).items():
        if not isinstance(record, dict):
            continue
        if int(record.get("target_id", 0)) != target_id:
            continue
        if str(record.get("action")) != "밴":
            continue
        records.append((int(record.get("case_id", 0)), key, record))
    if not records:
        return None, None
    _, key, record = max(records, key=lambda item: item[0])
    return key, record


async def fetch_recent_audit(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    seconds: int = 10,
) -> discord.AuditLogEntry | None:
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if not entry.target or getattr(entry.target, "id", None) != target_id:
                continue
            age = abs((dt.datetime.now(dt.timezone.utc) - entry.created_at).total_seconds())
            if age <= seconds:
                return entry
    except discord.Forbidden:
        return None
    except discord.DiscordException:
        return None
    return None


async def log_external_punishment(
    guild: discord.Guild,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None,
) -> int | None:
    config = load_config(guild.id)
    if recent_record_exists(config, action, target.id):
        return None
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, action, target, moderator, reason)
    save_config(guild.id, config)
    return case_id


async def punish_log(
    ctx: commands.Context,
    action: str,
    target: discord.abc.User,
    reason: str | None,
    color: int,
) -> discord.Embed:
    config = load_config(ctx.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, action, target, ctx.author, reason)
    save_config(ctx.guild.id, config)

    embed = punishment_embed("처벌 완료", get_case_code(config, case_id), action, target, ctx.author, reason, color)
    message = await send_log(ctx.guild, embed)
    if message:
        config = load_config(ctx.guild.id)
        attach_log_message(config, case_id, message)
        save_config(ctx.guild.id, config)
    return embed


async def log_timeout_expired(guild: discord.Guild, member: discord.Member, config: dict, timeout_data: dict | None = None) -> None:
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "언뮤트", member, guild.me, "Expired")
    save_config(guild.id, config)
    embed = timeout_release_embed(member, None, "Expired", get_case_code(config, case_id))
    await reply_to_log(guild, timeout_data, embed)


async def clear_pending_manual_unmute(key: tuple[int, int]) -> None:
    await asyncio.sleep(5)
    PENDING_MANUAL_UNMUTES.discard(key)


def cancel_timeout_release(guild_id: int, user_id: int) -> None:
    task = TIMEOUT_RELEASE_TASKS.pop((guild_id, user_id), None)
    if task and not task.done():
        task.cancel()


def schedule_timeout_release(guild_id: int, user_id: int, until_iso: str | None) -> None:
    cancel_timeout_release(guild_id, user_id)
    until = parse_time(until_iso)
    if not until:
        return

    async def runner() -> None:
        try:
            delay = max(0.0, (until.astimezone(dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).total_seconds())
            await asyncio.sleep(delay + 1)
            guild = bot.get_guild(guild_id)
            if not guild:
                return

            config = load_config(guild_id)
            timeout_data = config.get("active_timeouts", {}).get(str(user_id))
            if not isinstance(timeout_data, dict):
                return
            if (guild_id, user_id) in PENDING_MANUAL_UNMUTES:
                return

            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.DiscordException:
                    member = None

            now = dt.datetime.now(dt.timezone.utc)
            current_timeout = member.timed_out_until if member else None
            if current_timeout and current_timeout.astimezone(dt.timezone.utc) > now:
                schedule_timeout_release(guild_id, user_id, current_timeout.isoformat())
                return

            config["active_timeouts"].pop(str(user_id), None)
            save_config(guild_id, config)
            if member:
                await log_timeout_expired(guild, member, config, timeout_data)
        except asyncio.CancelledError:
            raise
        finally:
            if TIMEOUT_RELEASE_TASKS.get((guild_id, user_id)) is asyncio.current_task():
                TIMEOUT_RELEASE_TASKS.pop((guild_id, user_id), None)

    TIMEOUT_RELEASE_TASKS[(guild_id, user_id)] = bot.loop.create_task(runner())


def schedule_all_active_timeouts() -> None:
    for path in CONFIG_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue
        config = load_config(int(path.stem))
        for user_id, timeout_data in config.get("active_timeouts", {}).items():
            if str(user_id).isdigit() and isinstance(timeout_data, dict):
                schedule_timeout_release(int(path.stem), int(user_id), timeout_data.get("until"))


@tasks.loop(minutes=10)
async def check_timeout_expirations() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for path in CONFIG_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue

        guild = bot.get_guild(int(path.stem))
        if not guild:
            continue

        config = load_config(guild.id)
        changed = False
        for user_id, timeout_data in list(config.get("active_timeouts", {}).items()):
            until = parse_time(timeout_data.get("until") if isinstance(timeout_data, dict) else None)
            if not until or until.astimezone(dt.timezone.utc) > now:
                continue

            if not str(user_id).isdigit():
                config["active_timeouts"].pop(user_id, None)
                changed = True
                continue
            if (guild.id, int(user_id)) in PENDING_MANUAL_UNMUTES:
                continue

            member = guild.get_member(int(user_id)) if str(user_id).isdigit() else None
            if not member and str(user_id).isdigit():
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.DiscordException:
                    member = None

            config["active_timeouts"].pop(user_id, None)
            cancel_timeout_release(guild.id, int(user_id))
            changed = True
            current_timeout = member.timed_out_until if member else None
            still_timed_out = current_timeout and current_timeout.astimezone(dt.timezone.utc) > now
            if member and not still_timed_out:
                await log_timeout_expired(guild, member, config, timeout_data if isinstance(timeout_data, dict) else None)
                config = load_config(guild.id)

        if changed:
            save_config(guild.id, config)


@check_timeout_expirations.before_loop
async def before_check_timeout_expirations() -> None:
    await bot.wait_until_ready()


@bot.event
async def setup_hook() -> None:
    if not check_timeout_expirations.is_running():
        check_timeout_expirations.start()
    await bot.tree.sync()
    print("슬래시 명령어 동기화 완료")


@bot.event
async def on_ready() -> None:
    global SCHEDULED_TIMEOUTS_ON_READY
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}도움말 | 처벌 관리"))
    if not SCHEDULED_TIMEOUTS_ON_READY:
        schedule_all_active_timeouts()
        SCHEDULED_TIMEOUTS_ON_READY = True
    print(f"로그인 완료: {bot.user} ({bot.user.id})")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    before_timeout = before.timed_out_until
    after_timeout = after.timed_out_until

    if after_timeout is not None and (
        before_timeout is None
        or before_timeout.astimezone(dt.timezone.utc) != after_timeout.astimezone(dt.timezone.utc)
    ):
        await asyncio.sleep(1)
        config = load_config(after.guild.id)
        existing_timeout = config.get("active_timeouts", {}).get(str(after.id))

        entry = await fetch_recent_audit(after.guild, discord.AuditLogAction.member_update, after.id)
        if entry and entry.user and entry.user == after.guild.me:
            return
        if existing_timeout and not entry:
            stored_until = parse_time(existing_timeout.get("until") if isinstance(existing_timeout, dict) else None)
            if stored_until and abs((stored_until.astimezone(dt.timezone.utc) - after_timeout.astimezone(dt.timezone.utc)).total_seconds()) <= 3:
                return

        moderator = entry.user if entry and entry.user else after.guild.me
        reason = entry.reason if entry and entry.reason else "운영진 재량"
        duration_text = timeout_until_duration_text(after_timeout)
        action = f"뮤트 ({duration_text})"
        case_id = await log_external_punishment(after.guild, action, after, moderator, reason)
        config = load_config(after.guild.id)
        log_channel_id = existing_timeout.get("log_channel_id") if isinstance(existing_timeout, dict) else None
        log_message_id = existing_timeout.get("log_message_id") if isinstance(existing_timeout, dict) else None
        punishment_case_id = existing_timeout.get("punishment_case_id") if isinstance(existing_timeout, dict) else None
        config["active_timeouts"][str(after.id)] = {
            "until": after_timeout.isoformat(),
            "target_name": str(after),
            "reason": short_reason(reason),
            "punishment_case_id": punishment_case_id,
            "log_channel_id": log_channel_id,
            "log_message_id": log_message_id,
        }
        if case_id:
            embed = punishment_embed("처벌 완료", get_case_code(config, case_id), action, after, moderator, reason, TIMEOUT)
            message = await send_log(after.guild, embed)
            attach_log_message(config, case_id, message)
            if message:
                log_channel_id = message.channel.id
                log_message_id = message.id
            config["active_timeouts"][str(after.id)]["punishment_case_id"] = case_id
            config["active_timeouts"][str(after.id)]["log_channel_id"] = log_channel_id
            config["active_timeouts"][str(after.id)]["log_message_id"] = log_message_id
        save_config(after.guild.id, config)
        schedule_timeout_release(after.guild.id, after.id, after_timeout.isoformat())
        return

    if before_timeout is None or after_timeout is not None:
        return

    await asyncio.sleep(1)
    config = load_config(after.guild.id)
    timeout_data = config["active_timeouts"].pop(str(after.id), None)
    cancel_timeout_release(after.guild.id, after.id)
    if (after.guild.id, after.id) in PENDING_MANUAL_UNMUTES:
        save_config(after.guild.id, config)
        return
    entry = await fetch_recent_audit(after.guild, discord.AuditLogAction.member_update, after.id)
    if entry and entry.user and entry.user != after.guild.me:
        if not timeout_data:
            return
        moderator = entry.user
        release_reason = entry.reason or "운영진 재량"
    else:
        moderator = after.guild.me
        release_reason = "Expired"

    save_config(after.guild.id, config)
    config = load_config(after.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "언뮤트", after, moderator, release_reason)
    save_config(after.guild.id, config)
    embed = timeout_release_embed(after, None if release_reason == "Expired" else moderator, release_reason, get_case_code(config, case_id))
    await reply_to_log(after.guild, timeout_data if isinstance(timeout_data, dict) else None, embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await asyncio.sleep(1)
    entry = await fetch_recent_audit(member.guild, discord.AuditLogAction.kick, member.id)
    if not entry:
        return

    moderator = entry.user if entry.user else member.guild.me
    reason = entry.reason or "운영진 재량"
    case_id = await log_external_punishment(member.guild, "킥", member, moderator, reason)
    if case_id:
        config = load_config(member.guild.id)
        embed = removal_embed(get_case_code(config, case_id), "추방", member, moderator, reason, KICK)
        await send_log(member.guild, embed)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    await asyncio.sleep(1)
    entry = await fetch_recent_audit(guild, discord.AuditLogAction.ban, user.id)
    moderator = entry.user if entry and entry.user else guild.me
    reason = entry.reason if entry and entry.reason else "운영진 재량"
    case_id = await log_external_punishment(guild, "밴", user, moderator, reason)
    if case_id:
        config = load_config(guild.id)
        embed = ban_action_embed(guild, get_case_code(config, case_id), "밴", user, moderator, reason)
        message = await send_log(guild, embed)
        attach_log_message(config, case_id, message)
        save_config(guild.id, config)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    await asyncio.sleep(1)
    entry = await fetch_recent_audit(guild, discord.AuditLogAction.unban, user.id)
    moderator = entry.user if entry and entry.user else guild.me
    reason = entry.reason if entry and entry.reason else "운영진 재량"
    case_id = await log_external_punishment(guild, "언밴", user, moderator, reason)
    if case_id:
        config = load_config(guild.id)
        _, ban_record = latest_ban_record(config, user.id)
        embed = ban_action_embed(guild, get_case_code(config, case_id), "언밴", user, moderator, reason)
        await reply_to_log(guild, ban_record, embed)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(embed=make_embed("권한 부족", "이 명령어를 사용할 권한이 없어요.", RED), mention_author=False)
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply(embed=make_embed("권한 부족", "관리자 또는 등록된 운영팀 역할만 사용할 수 있어요.", RED), mention_author=False)
        return
    if isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await ctx.reply(embed=make_embed("봇 권한 부족", f"봇에게 필요한 권한: `{missing}`", RED), mention_author=False)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(embed=make_embed("입력 오류", "유저, 시간, 숫자 형식을 다시 확인해 주세요.", RED), mention_author=False)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(embed=make_embed("입력 부족", f"`{PREFIX}도움말`에서 사용법을 확인해 주세요.", RED), mention_author=False)
        return

    print(f"명령어 오류: {type(error).__name__}: {error}")
    await ctx.reply(embed=make_embed("오류", "명령어 처리 중 문제가 발생했어요.", RED), mention_author=False)


def help_embed(guild: discord.Guild, category: str = "home") -> discord.Embed:
    colors = {
        "home": HELP,
        "basic": HELP,
        "warn": HELP_WARN,
        "punish": HELP_PUNISH,
        "setting": HELP_SETTING,
        "edit": BLUE,
        "rule": CARD,
    }
    embed = make_embed(
        "Opal Help",
        None,
        colors.get(category, HELP),
    )
    embed.set_author(name=f"{guild.name} 관리 도구", icon_url=guild.icon.url if guild.icon else None)

    if category == "basic":
        embed.description = "봇 상태와 기본 설정"
        embed.add_field(name="명령어", value=f"`{PREFIX}핑`\n`{PREFIX}업타임`\n`{PREFIX}로그채널 #채널`", inline=False)
    elif category == "warn":
        embed.description = "경고 지급, 조회, 삭제"
        embed.add_field(
            name="Prefix",
            value=(
                f"`{PREFIX}경고 @유저 [사유]`\n"
                f"`{PREFIX}경고목록 @유저`\n"
                f"`{PREFIX}경고삭제 처벌코드`\n"
                f"`{PREFIX}경고삭제 @유저 처벌코드`\n"
                f"`{PREFIX}경고초기화 @유저`"
            ),
            inline=False,
        )
        embed.add_field(name="Slash", value="`/경고조회`\n`/경고자동처벌`", inline=False)
    elif category == "punish":
        embed.description = "타임아웃, 추방, 차단"
        embed.add_field(
            name="명령어",
            value=(
                f"`{PREFIX}뮤트 @유저 10분 [사유]`\n"
                f"`{PREFIX}언뮤트 @유저 [사유]`\n"
                f"`{PREFIX}킥 유저ID [사유]`\n"
                f"`{PREFIX}밴 유저ID [사유]`\n"
                f"`{PREFIX}언밴 유저ID [사유]`\n"
                f"`{PREFIX}청소 10`"
            ),
            inline=False,
        )
        embed.add_field(name="참고", value="밴은 서버 밖 유저 ID도 처리할 수 있어요.", inline=False)
    elif category == "setting":
        embed.description = "서버별 운영 설정"
        embed.add_field(
            name="Slash",
            value=(
                "`/로그채널지정`\n"
                "`/기본타임아웃지정`\n"
                "`/운영팀추가`\n"
                "`/운영팀처벌강도설정`\n"
                "`/임베드이모티콘수정`"
            ),
            inline=False,
        )
    elif category == "edit":
        embed.description = "처벌코드로 기존 기록 수정"
        embed.add_field(
            name="명령어",
            value=(
                f"`{PREFIX}수정 처벌코드 새사유`\n"
                f"`{PREFIX}수정 처벌코드 새사유 30분`"
            ),
            inline=False,
        )
        embed.add_field(name="참고", value="타임아웃 시간 수정은 현재 남은 시간에 더해서 적용됩니다.", inline=False)
    elif category == "rule":
        embed.description = "입력 규칙과 기본값"
        embed.add_field(
            name="기본값",
            value=(
                "사유 생략 시 `운영진 재량`\n"
                "시간 생략 시 기본 `10분` 또는 서버 설정값"
            ),
            inline=False,
        )
        embed.add_field(
            name="시간",
            value=(
                "`30초`, `10분`, `2시간`, `1일`, `1주`\n"
                "`하루`, `일주일`, `30s`, `10m`, `2h`, `1d`, `1w`\n\n"
                "청소는 처벌 로그에 남기지 않습니다."
            ),
            inline=False,
        )
    else:
        embed.description = "메뉴에서 필요한 항목만 골라 확인하세요."
        embed.add_field(name="핵심", value="처벌코드 중심 로그 관리\n멘션/유저 ID 둘 다 지원\n타임아웃 자동해제 로그 지원", inline=False)
        embed.add_field(name="항목", value="`기본` `경고` `처벌` `설정` `기록 수정` `규칙`", inline=False)

    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="처음", value="home", description="도움말 첫 화면"),
            discord.SelectOption(label="기본", value="basic", description="핑, 업타임, 로그채널"),
            discord.SelectOption(label="경고", value="warn", description="경고 지급/조회/삭제/자동처벌"),
            discord.SelectOption(label="처벌", value="punish", description="뮤트, 언뮤트, 킥, 밴, 청소"),
            discord.SelectOption(label="설정", value="setting", description="슬래시 설정 명령어"),
            discord.SelectOption(label="기록 수정", value="edit", description="처벌코드 수정"),
            discord.SelectOption(label="규칙", value="rule", description="사유/시간 기본값"),
        ]
        super().__init__(placeholder="도움말 항목 선택", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not interaction.guild:
            return
        await interaction.response.edit_message(embed=help_embed(interaction.guild, self.values[0]), view=view)


class HelpView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.add_item(HelpSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(embed=make_embed("사용 불가", "이 도움말 메뉴는 명령어를 실행한 사람만 사용할 수 있어요.", RED), ephemeral=True)
        return False


@bot.command(name="도움말", aliases=["help", "명령어"])
async def help_command(ctx: commands.Context) -> None:
    await ctx.reply(embed=help_embed(ctx.guild), view=HelpView(ctx.author.id), mention_author=False)


def warning_record_code(warning: dict) -> str:
    return case_id_text(warning.get("case_code") or warning.get("case_id") or "-")


def warning_record_time(warning: dict) -> str:
    return str(warning.get("created_at", ""))[:16].replace("T", " ") or "-"


def warning_record_embed(
    guild: discord.Guild,
    member: discord.Member,
    warnings: list[dict],
    page: int = 0,
    selected_index: int | None = None,
) -> discord.Embed:
    embed = make_embed("경고 기록", color=HELP_WARN)
    embed.set_author(name=f"{display_name(member)} 경고 컬렉션", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="대상자", value=member.mention, inline=True)
    embed.add_field(name="경고누적", value=f"`{len(warnings)} / {warning_limit_for(guild, len(warnings))}`", inline=True)

    if selected_index is not None and 0 <= selected_index < len(warnings):
        warning = warnings[selected_index]
        moderator = guild.get_member(int(warning.get("moderator_id", 0)))
        mod_text = moderator.mention if moderator else f"`{warning.get('moderator_id', '-')}`"
        embed.add_field(name="처벌코드", value=f"`{warning_record_code(warning)}`", inline=True)
        embed.add_field(name="지급일", value=f"`{warning_record_time(warning)}`", inline=True)
        embed.add_field(name="담당자", value=mod_text, inline=False)
        embed.add_field(name="사유", value=f"```{code_block_text(warning.get('reason'))}```", inline=False)
        embed.set_footer(text="목록에서 다른 경고를 선택할 수 있어요.")
        return embed

    page_size = 10
    total_pages = max(1, (len(warnings) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    shown = warnings[start:start + page_size]
    lines = []
    for index, warning in enumerate(shown, start=start + 1):
        reason = short_reason(warning.get("reason"))[:45]
        lines.append(f"`{index}.` `{warning_record_code(warning)}` {reason}\n`{warning_record_time(warning)}`")
    embed.add_field(name=f"목록 {page + 1}/{total_pages}", value="\n\n".join(lines) or "경고 기록이 없어요.", inline=False)
    embed.set_footer(text="아래 메뉴에서 경고 기록을 선택하세요.")
    return embed


class WarningRecordSelect(discord.ui.Select):
    def __init__(self, view: "WarningRecordView") -> None:
        start = view.page * view.page_size
        shown = view.warnings[start:start + view.page_size]
        options = []
        for offset, warning in enumerate(shown):
            absolute_index = start + offset
            options.append(discord.SelectOption(
                label=f"{absolute_index + 1}. {warning_record_code(warning)}",
                value=str(absolute_index),
                description=short_reason(warning.get("reason"))[:90],
            ))
        super().__init__(placeholder="경고 기록 선택", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, WarningRecordView) or not interaction.guild:
            return
        selected_index = int(self.values[0])
        await interaction.response.edit_message(
            embed=warning_record_embed(interaction.guild, view.member, view.warnings, view.page, selected_index),
            view=view,
        )


class WarningPageButton(discord.ui.Button):
    def __init__(self, label: str, direction: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, WarningRecordView) or not interaction.guild:
            return
        view.page = max(0, min(view.max_page, view.page + self.direction))
        view.refresh_items()
        await interaction.response.edit_message(
            embed=warning_record_embed(interaction.guild, view.member, view.warnings, view.page),
            view=view,
        )


class WarningRecordView(discord.ui.View):
    page_size = 10

    def __init__(self, author_id: int, member: discord.Member, warnings: list[dict]) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.member = member
        self.warnings = warnings
        self.page = 0
        self.refresh_items()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.warnings) + self.page_size - 1) // self.page_size - 1)

    def refresh_items(self) -> None:
        self.clear_items()
        self.add_item(WarningRecordSelect(self))
        if self.max_page > 0:
            previous_button = WarningPageButton("이전", -1)
            previous_button.disabled = self.page <= 0
            next_button = WarningPageButton("다음", 1)
            next_button.disabled = self.page >= self.max_page
            self.add_item(previous_button)
            self.add_item(next_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(embed=make_embed("사용 불가", "이 경고 기록 메뉴는 명령어를 실행한 사람만 사용할 수 있어요.", RED), ephemeral=True)
        return False


@bot.command(name="핑", aliases=["ping"])
async def ping(ctx: commands.Context) -> None:
    await ctx.reply(embed=make_embed("퐁", f"지연 시간: `{round(bot.latency * 1000)}ms`", GREEN), mention_author=False)


@bot.command(name="업타임", aliases=["uptime"])
async def uptime(ctx: commands.Context) -> None:
    embed = make_embed("업타임", f"`{uptime_text()}` 동안 실행 중이에요.", GREEN)
    embed.add_field(name="지연 시간", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    embed.add_field(name="시작 시각", value=f"`{STARTED_AT.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')}`", inline=True)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="info", aliases=["ifo"])
async def debug_info(ctx: commands.Context) -> None:
    if not await is_developer(ctx.author):
        await ctx.reply(embed=make_embed("권한 부족", "개발자 디버깅 전용 명령어예요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    active_tasks = sum(1 for task in TIMEOUT_RELEASE_TASKS.values() if not task.done())
    embed = make_embed("IFO Debug", color=DARK)
    embed.add_field(name="Bot", value=f"`{bot.user}`\n`{bot.user.id if bot.user else '-'}`", inline=True)
    embed.add_field(name="Runtime", value=f"업타임 `{uptime_text()}`\n핑 `{round(bot.latency * 1000)}ms`", inline=True)
    embed.add_field(name="Versions", value=f"Python `{platform.python_version()}`\ndiscord.py `{discord.__version__}`", inline=True)
    embed.add_field(name="Guild", value=f"`{ctx.guild.name}`\n`{ctx.guild.id}`", inline=True)
    embed.add_field(name="Counts", value=f"서버 `{len(bot.guilds)}`개\n유저 캐시 `{len(bot.users)}`명\n명령어 `{len(bot.commands)}`개", inline=True)
    embed.add_field(name="Config", value=f"처벌 `{len(config.get('punishments', {}))}`개\n경고유저 `{len(config.get('warnings', {}))}`명\n예약해제 `{active_tasks}`개", inline=True)
    embed.add_field(name="System", value=f"`{platform.system()} {platform.release()}`\n`{sys.executable}`", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="로그채널", aliases=["로그채널설정", "setlog"])
@commands.has_permissions(manage_guild=True)
async def set_log_channel(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    channel = channel or ctx.channel
    config = load_config(ctx.guild.id)
    config["log_channel"] = channel.id
    save_config(ctx.guild.id, config)
    await ctx.reply(embed=make_embed("로그 채널 설정 완료", f"처벌 로그를 {channel.mention}에 남길게요.", GREEN), mention_author=False)


@bot.tree.command(name="로그채널지정", description="처벌 로그가 올라갈 채널을 지정합니다.")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not interaction.guild:
        return

    config = load_config(interaction.guild.id)
    config["log_channel"] = channel.id
    save_config(interaction.guild.id, config)
    embed = make_embed("로그 채널 지정 완료", f"처벌 로그를 {channel.mention}에 남길게요.", GREEN)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="기본타임아웃지정", description="뮤트 시간이 생략됐을 때 사용할 기본 시간을 지정합니다.")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_default_timeout(interaction: discord.Interaction, 시간: str) -> None:
    if not interaction.guild:
        return

    delta = parse_duration(시간)
    if not delta:
        embed = make_embed("시간 형식 오류", "예: `10분`, `2시간`, `1일`, `1주`, `하루`, `일주일`, `10m`, `2h`, `1w`", RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if delta > dt.timedelta(days=28):
        embed = make_embed("시간 초과", "디스코드 타임아웃은 최대 28일까지 가능해요.", RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    seconds = int(delta.total_seconds())
    config = load_config(interaction.guild.id)
    config["default_timeout_seconds"] = seconds
    save_config(interaction.guild.id, config)
    embed = make_embed("기본 타임아웃 지정 완료", f"기본 타임아웃을 `{seconds_to_duration_text(seconds)}`로 설정했어요.", GREEN)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="운영팀추가", description="봇 처벌 명령어를 사용할 운영팀 역할을 추가합니다.")
@app_commands.default_permissions(manage_guild=True)
async def slash_add_staff_role(interaction: discord.Interaction, 역할: discord.Role) -> None:
    if not interaction.guild:
        return

    config = load_config(interaction.guild.id)
    staff_roles = set(config.get("staff_roles", []))
    staff_roles.add(역할.id)
    config["staff_roles"] = sorted(staff_roles)
    save_config(interaction.guild.id, config)

    embed = make_embed("운영팀 추가 완료", color=GREEN)
    embed.add_field(name="추가된 역할", value=역할.mention, inline=False)
    embed.add_field(name="등록된 운영팀", value=f"`{len(config['staff_roles'])}`개 역할", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="임베드이모티콘수정", description="처벌 로그 임베드에 표시되는 이모티콘을 수정합니다.")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_embed_emojis(
    interaction: discord.Interaction,
    타임아웃: str | None = None,
    타임아웃해제: str | None = None,
    추방: str | None = None,
    차단: str | None = None,
    차단해제: str | None = None,
    밴해제: str | None = None,
    경고: str | None = None,
    경고삭제: str | None = None,
    경고초기화: str | None = None,
    수정: str | None = None,
) -> None:
    if not interaction.guild:
        return

    config = load_config(interaction.guild.id)
    emojis = config.setdefault("embed_emojis", DEFAULT_EMBED_EMOJIS.copy())
    updates = {
        "timeout": 타임아웃,
        "untimeout": 타임아웃해제,
        "kick": 추방,
        "ban": 차단,
        "unban": 밴해제 or 차단해제,
        "warn": 경고,
        "warn_delete": 경고삭제,
        "warn_clear": 경고초기화,
        "edit": 수정,
    }
    changed = []
    for key, value in updates.items():
        if value is None:
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        emojis[key] = cleaned[:40]
        changed.append(key)

    if not changed:
        embed = make_embed("변경 없음", "수정할 이모티콘을 하나 이상 입력해 주세요. 입력하지 않은 항목은 기존 값이 유지돼요.", YELLOW)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    save_config(interaction.guild.id, config)
    embed = make_embed("임베드 이모티콘 수정 완료", color=GREEN)
    embed.add_field(
        name="현재 설정",
        value=(
            f"{emojis['timeout']} 타임아웃\n"
            f"{emojis['untimeout']} 타임아웃 해제\n"
            f"{emojis['kick']} 추방\n"
            f"{emojis['ban']} 차단\n"
            f"{emojis['unban']} 밴 해제\n"
            f"{emojis['warn']} 경고\n"
            f"{emojis['warn_delete']} 경고 삭제\n"
            f"{emojis['warn_clear']} 경고 초기화\n"
            f"{emojis['edit']} 수정"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="운영팀처벌강도설정", description="운영팀 역할이 사용할 수 있는 처벌을 선택합니다.")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_staff_limit(
    interaction: discord.Interaction,
    역할: discord.Role,
    전체: bool = False,
    경고: bool = False,
    타임아웃: bool = False,
    타임아웃해제: bool = False,
    추방: bool = False,
    차단: bool = False,
    차단해제: bool = False,
    청소: bool = False,
    최대타임아웃: str | None = None,
) -> None:
    if not interaction.guild:
        return

    config = load_config(interaction.guild.id)
    staff_roles = set(config.get("staff_roles", []))
    staff_roles.add(역할.id)
    config["staff_roles"] = sorted(staff_roles)

    role_id = str(역할.id)
    role_limit = config.setdefault("role_punishment_limits", {}).setdefault(role_id, {
        "actions": [],
        "timeout_seconds": None,
    })
    if 전체:
        actions = set(PUNISHMENT_ACTIONS)
    else:
        selected = {
            "warn": 경고,
            "timeout": 타임아웃,
            "untimeout": 타임아웃해제,
            "kick": 추방,
            "ban": 차단,
            "unban": 차단해제,
            "purge": 청소,
        }
        actions = {action for action, enabled in selected.items() if enabled}

    if not actions:
        embed = make_embed(
            "설정 실패",
            "최소 하나 이상의 처벌을 선택해 주세요. 선택한 처벌만 이 역할에 허용돼요.",
            RED,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    timeout_seconds = role_limit.get("timeout_seconds")
    if "timeout" in actions and 최대타임아웃:
        delta = parse_duration(최대타임아웃)
        if not delta:
            embed = make_embed("시간 형식 오류", "예: `30분`, `2시간`, `1일`, `1주`, `하루`, `일주일`, `30m`, `2h`, `1w`", RED)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if delta > dt.timedelta(days=28):
            embed = make_embed("시간 초과", "디스코드 타임아웃은 최대 28일까지 가능해요.", RED)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        timeout_seconds = int(delta.total_seconds())
        config.setdefault("role_timeout_limits", {})[role_id] = timeout_seconds
    elif "timeout" not in actions:
        timeout_seconds = None
        config.setdefault("role_timeout_limits", {}).pop(role_id, None)

    role_limit["actions"] = sorted(actions)
    role_limit["timeout_seconds"] = timeout_seconds
    save_config(interaction.guild.id, config)

    embed = make_embed("운영팀 처벌 강도 설정 완료", color=GREEN)
    embed.add_field(name="역할", value=역할.mention, inline=False)
    embed.add_field(name="허용 처벌", value=", ".join(f"`{PUNISHMENT_LABELS[action]}`" for action in role_limit["actions"]), inline=False)
    timeout_text = seconds_to_duration_text(timeout_seconds) if timeout_seconds else "제한 없음"
    embed.add_field(name="최대 타임아웃", value=f"`{timeout_text}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="경고조회", description="유저의 경고 기록을 조회합니다.")
@app_commands.default_permissions(moderate_members=True)
async def slash_warning_lookup(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if not interaction.guild:
        return
    if not await require_interaction_punishment_power(interaction, "warn"):
        return

    config = load_config(interaction.guild.id)
    user_warnings = config["warnings"].get(str(유저.id), [])
    if not user_warnings:
        embed = make_embed("경고 조회", f"{유저.mention}님의 경고 기록이 없어요.", GREEN)
        embed.set_thumbnail(url=유저.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    view = WarningRecordView(interaction.user.id, 유저, user_warnings)
    await interaction.response.send_message(
        embed=warning_record_embed(interaction.guild, 유저, user_warnings),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="경고지급", description="유저에게 경고를 지급합니다.")
@app_commands.default_permissions(moderate_members=True)
async def slash_warn(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    if not await require_interaction_punishment_power(interaction, "warn"):
        return

    fake_ctx = InteractionCommandContext(interaction)
    ok, message = can_target(fake_ctx, 유저)
    if not ok:
        await interaction.response.send_message(embed=make_embed("처벌 불가", message, RED), ephemeral=True)
        return

    config = load_config(interaction.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "경고", 유저, interaction.user, 사유)
    case_code = get_case_code(config, case_id)
    warning = {
        "case_id": case_id,
        "case_code": case_code,
        "moderator_id": interaction.user.id,
        "reason": short_reason(사유),
        "created_at": now_kst().isoformat(),
    }
    config["warnings"].setdefault(str(유저.id), []).append(warning)
    save_config(interaction.guild.id, config)

    total = len(config["warnings"][str(유저.id)])
    embed = warning_embed(case_code, 유저, interaction.user, 사유, total)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await send_log(interaction.guild, embed)
    await apply_warning_auto_rules(fake_ctx, 유저, total)


@bot.tree.command(name="경고삭제", description="처벌코드로 경고 기록을 삭제합니다.")
@app_commands.default_permissions(moderate_members=True)
async def slash_delete_warning(interaction: discord.Interaction, 처벌코드: str, 유저: discord.Member | None = None, 사유: str | None = None) -> None:
    if not interaction.guild:
        return
    if not await require_interaction_punishment_power(interaction, "warn"):
        return

    config = load_config(interaction.guild.id)
    target_id = 유저.id if 유저 else None
    user_id, index, warning, punishment_key, punishment_record = find_warning_record(config, 처벌코드, target_id)
    if user_id is None or index is None or warning is None:
        await interaction.response.send_message(embed=make_embed("경고 삭제 실패", f"`{처벌코드}` 경고 처벌코드를 찾지 못했어요.", RED), ephemeral=True)
        return
    if str(user_id) == str(interaction.user.id):
        await interaction.response.send_message(embed=make_embed("경고 삭제 실패", "자기 자신의 경고 기록은 삭제할 수 없어요.", RED), ephemeral=True)
        return

    user_warnings = config["warnings"].get(user_id, [])
    if not user_warnings or index >= len(user_warnings):
        await interaction.response.send_message(embed=make_embed("경고 삭제 실패", "삭제할 경고 기록이 없어요.", RED), ephemeral=True)
        return
    removed_warning = user_warnings.pop(index)
    if not user_warnings:
        config["warnings"].pop(user_id, None)
    if punishment_key and punishment_record:
        punishment_record["deleted_at"] = now_kst().isoformat()
        punishment_record["deleted_by_id"] = interaction.user.id
        punishment_record["deleted_by_name"] = str(interaction.user)

    save_config(interaction.guild.id, config)
    member = 유저 or interaction.guild.get_member(int(user_id))
    removed_code = removed_warning.get("case_code") or removed_warning.get("case_id") or 처벌코드
    after = len(config["warnings"].get(user_id, []))

    embed = warning_action_embed(
        interaction.guild,
        "경고 삭제",
        member,
        interaction.user,
        사유,
        f"삭제 1개 / 남은 경고 {after}회",
        str(removed_code),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await send_log(interaction.guild, embed)


@bot.tree.command(name="경고초기화", description="유저의 경고를 모두 초기화합니다.")
@app_commands.default_permissions(moderate_members=True)
async def slash_clear_warnings(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None) -> None:
    if not interaction.guild:
        return
    if not await require_interaction_punishment_power(interaction, "warn"):
        return
    if 유저.id == interaction.user.id:
        await interaction.response.send_message(embed=make_embed("경고 초기화 실패", "자기 자신의 경고 기록은 초기화할 수 없어요.", RED), ephemeral=True)
        return

    config = load_config(interaction.guild.id)
    removed = len(config["warnings"].get(str(유저.id), []))
    if removed <= 0:
        await interaction.response.send_message(embed=make_embed("경고 초기화 실패", f"{유저.mention}님의 초기화할 경고 기록이 없어요.", RED), ephemeral=True)
        return
    config["warnings"][str(유저.id)] = []
    save_config(interaction.guild.id, config)

    embed = warning_action_embed(interaction.guild, "경고 초기화", 유저, interaction.user, 사유, f"초기화 {removed}개")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await send_log(interaction.guild, embed)


@bot.tree.command(name="경고자동처벌", description="경고 누적 수에 따른 자동처벌 규칙을 추가합니다.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(처벌=[
    app_commands.Choice(name="타임아웃", value="timeout"),
    app_commands.Choice(name="추방", value="kick"),
    app_commands.Choice(name="차단", value="ban"),
])
async def slash_warning_auto_rule(
    interaction: discord.Interaction,
    경고수: int,
    처벌: app_commands.Choice[str],
    시간: str | None = None,
) -> None:
    if not interaction.guild:
        return
    if 경고수 < 1:
        await interaction.response.send_message(embed=make_embed("설정 실패", "경고수는 1 이상이어야 해요.", RED), ephemeral=True)
        return

    duration_seconds = 600
    if 처벌.value == "timeout":
        delta = parse_duration(시간 or "10분")
        if not delta:
            await interaction.response.send_message(embed=make_embed("시간 형식 오류", "예: `10분`, `2시간`, `1일`, `1주`, `하루`, `일주일`", RED), ephemeral=True)
            return
        if delta > dt.timedelta(days=28):
            await interaction.response.send_message(embed=make_embed("시간 초과", "디스코드 타임아웃은 최대 28일까지 가능해요.", RED), ephemeral=True)
            return
        duration_seconds = int(delta.total_seconds())

    config = load_config(interaction.guild.id)
    config["warning_auto_rules"].append({
        "threshold": 경고수,
        "action": 처벌.value,
        "duration_seconds": duration_seconds,
    })
    save_config(interaction.guild.id, config)

    action_text = {"timeout": "타임아웃", "kick": "추방", "ban": "차단"}[처벌.value]
    detail = f" / `{seconds_to_duration_text(duration_seconds)}`" if 처벌.value == "timeout" else ""
    embed = make_embed("경고 자동처벌 추가 완료", f"`{경고수}`회 -> `{action_text}`{detail}", GREEN)
    embed.add_field(name="등록된 규칙", value=f"`{len(config['warning_auto_rules'])}`개", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def apply_warning_auto_rules(ctx: commands.Context, member: discord.Member, total: int) -> None:
    config = load_config(ctx.guild.id)
    rules = [rule for rule in config.get("warning_auto_rules", []) if rule.get("threshold") == total]
    if not rules:
        return

    for rule in rules:
        reason = f"경고 {total}회 누적 자동처벌"
        action = rule.get("action")
        try:
            if action == "timeout":
                seconds = int(rule.get("duration_seconds", 600))
                until = stacked_timeout_until(member, dt.timedelta(seconds=seconds))
                max_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=28)
                if until > max_until:
                    until = max_until
                await member.timeout(until, reason=reason)
                duration_text = timeout_until_duration_text(until)
                embed = await punish_log(ctx, f"뮤트 ({duration_text})", member, reason, TIMEOUT)
                config = load_config(ctx.guild.id)
                timeout_case_id, timeout_record = latest_timeout_record(config, member.id)
                config["active_timeouts"][str(member.id)] = {
                    "until": until.isoformat(),
                    "target_name": str(member),
                    "reason": reason,
                    "punishment_case_id": timeout_case_id,
                    "log_channel_id": timeout_record.get("log_channel_id") if timeout_record else None,
                    "log_message_id": timeout_record.get("log_message_id") if timeout_record else None,
                }
                save_config(ctx.guild.id, config)
                schedule_timeout_release(ctx.guild.id, member.id, until.isoformat())
                await ctx.send(embed=embed)
            elif action == "kick":
                config = load_config(ctx.guild.id)
                case_id = next_case_id(config)
                save_punishment_record(config, case_id, "킥", member, ctx.author, reason)
                save_config(ctx.guild.id, config)
                embed = removal_embed(get_case_code(config, case_id), "추방", member, ctx.author, reason, KICK)
                await member.kick(reason=reason)
                await send_log(ctx.guild, embed)
                await ctx.send(embed=embed)
                break
            elif action == "ban":
                config = load_config(ctx.guild.id)
                case_id = next_case_id(config)
                save_punishment_record(config, case_id, "밴", member, ctx.author, reason)
                save_config(ctx.guild.id, config)
                embed = ban_action_embed(ctx.guild, get_case_code(config, case_id), "밴", member, ctx.author, reason)
                await member.ban(reason=reason, delete_message_days=0)
                message = await send_log(ctx.guild, embed)
                config = load_config(ctx.guild.id)
                attach_log_message(config, case_id, message)
                save_config(ctx.guild.id, config)
                await ctx.send(embed=embed)
                break
        except discord.DiscordException:
            await ctx.send(embed=make_embed("자동처벌 실패", f"{member.mention} 자동처벌을 처리하지 못했어요.", RED))


@bot.command(name="경고", aliases=["warn"])
@commands.check(can_use_moderation)
async def warn(ctx: commands.Context, target: str, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "warn"):
        return

    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    ok, message = can_target(ctx, member)
    if not ok:
        await ctx.reply(embed=make_embed("처벌 불가", message, RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "경고", member, ctx.author, reason)
    case_code = get_case_code(config, case_id)
    warning = {
        "case_id": case_id,
        "case_code": case_code,
        "moderator_id": ctx.author.id,
        "reason": short_reason(reason),
        "created_at": now_kst().isoformat(),
    }
    config["warnings"].setdefault(str(member.id), []).append(warning)
    save_config(ctx.guild.id, config)

    total = len(config["warnings"][str(member.id)])
    embed = warning_embed(case_code, member, ctx.author, reason, total)
    await ctx.reply(embed=embed, mention_author=False)
    await send_log(ctx.guild, embed)
    await apply_warning_auto_rules(ctx, member, total)


@bot.command(name="경고목록", aliases=["warnings", "warns"])
@commands.check(can_use_moderation)
async def warnings(ctx: commands.Context, target: str) -> None:
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    user_warnings = config["warnings"].get(str(member.id), [])
    if not user_warnings:
        embed = make_embed("경고 목록", f"{member.mention}님의 경고가 없어요.", GREEN)
        await ctx.reply(embed=embed, mention_author=False)
        return

    view = WarningRecordView(ctx.author.id, member, user_warnings)
    await ctx.reply(embed=warning_record_embed(ctx.guild, member, user_warnings), view=view, mention_author=False)


@bot.command(name="수정", aliases=["editcase", "caseedit"])
@commands.check(can_use_moderation)
async def edit_punishment(ctx: commands.Context, case_id: str, *, changes: str = "") -> None:
    input_code = case_id.strip().lstrip("#")
    if not input_code:
        await ctx.reply(embed=make_embed("수정 실패", "처벌코드를 입력해 주세요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    clean_case_id, record = find_punishment_record(config, input_code)
    if not record:
        await ctx.reply(embed=make_embed("수정 실패", f"`{input_code}` 처벌 기록을 찾지 못했어요.", RED), mention_author=False)
        return
    if "경고" in str(record.get("action", "")) and int(record.get("target_id", 0)) == ctx.author.id:
        await ctx.reply(embed=make_embed("수정 실패", "자기 자신의 경고 기록은 수정할 수 없어요.", RED), mention_author=False)
        return

    old_reason = record.get("reason", "운영진 재량")
    old_action = record.get("action", "처벌")
    new_reason = None
    new_duration = None
    new_duration_text = None
    duration_applied = False

    parts = changes.rsplit(maxsplit=1)
    if changes:
        last = parts[-1]
        parsed = parse_duration(last)
        if parsed:
            new_duration = parsed
            new_duration_text = seconds_to_duration_text(int(parsed.total_seconds()))
            new_reason = parts[0].strip() if len(parts) > 1 else None
        else:
            new_reason = changes.strip()

    if new_reason:
        record["reason"] = short_reason(new_reason)
    else:
        record["reason"] = short_reason(record.get("reason"))

    if new_duration_text and "뮤트" not in old_action:
        await ctx.reply(embed=make_embed("수정 실패", "타임아웃 시간 수정은 타임아웃 처벌 기록에서만 가능해요.", RED), mention_author=False)
        return

    if new_duration_text and "뮤트" in old_action:
        duration_applied = True
        member = ctx.guild.get_member(int(record.get("target_id", 0)))
        if member:
            until = stacked_timeout_until(member, new_duration)
            max_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=28)
            if until > max_until:
                await ctx.reply(embed=make_embed("수정 실패", "디스코드 타임아웃은 현재 시점 기준 최대 28일까지 가능해요.", RED), mention_author=False)
                return
            new_duration_text = timeout_until_duration_text(until)
            record["action"] = update_action_duration(old_action, new_duration_text)
            await member.timeout(until, reason=record["reason"])
            active_config = load_config(ctx.guild.id)
            active_config["active_timeouts"][str(member.id)] = {
                "until": until.isoformat(),
                "target_name": str(member),
                "reason": record["reason"],
                "punishment_case_id": clean_case_id,
                "log_channel_id": record.get("log_channel_id"),
                "log_message_id": record.get("log_message_id"),
            }
            active_config["punishments"][clean_case_id] = record
            config = active_config
            schedule_timeout_release(ctx.guild.id, member.id, until.isoformat())
        else:
            record["action"] = update_action_duration(old_action, new_duration_text)

    record["updated_at"] = now_kst().isoformat()
    record["updated_by_id"] = ctx.author.id
    record["updated_by_name"] = str(ctx.author)

    target_id = str(record.get("target_id"))
    for warning in config.get("warnings", {}).get(target_id, []):
        if str(warning.get("case_id")) == clean_case_id:
            warning["reason"] = record["reason"]

    save_config(ctx.guild.id, config)

    shown_code = record.get("case_code", clean_case_id)
    embed = edit_embed(
        ctx.guild,
        shown_code,
        record,
        ctx.author,
        str(old_reason),
        new_duration_text if duration_applied else None,
        action_duration(old_action) if duration_applied else None,
    )
    await ctx.reply(embed=embed, mention_author=False)
    await reply_to_log(ctx.guild, record, embed)


@bot.command(name="경고삭제", aliases=["delwarn", "경고제거"])
@commands.check(can_use_moderation)
async def delete_warning(ctx: commands.Context, target_or_code: str, case_code: str | None = None, *, reason: str | None = None) -> None:
    config = load_config(ctx.guild.id)
    member = None
    target_id = None
    code = target_or_code

    if case_code is not None:
        member = await resolve_member(ctx, target_or_code)
        if not member:
            await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
            return
        target_id = member.id
        code = case_code

    user_id, index, warning, punishment_key, punishment_record = find_warning_record(config, code, target_id)
    if user_id is None or index is None or warning is None:
        await ctx.reply(embed=make_embed("경고 삭제 실패", f"`{code}` 경고 처벌코드를 찾지 못했어요.", RED), mention_author=False)
        return
    if str(user_id) == str(ctx.author.id):
        await ctx.reply(embed=make_embed("경고 삭제 실패", "자기 자신의 경고 기록은 삭제할 수 없어요.", RED), mention_author=False)
        return

    user_warnings = config["warnings"].get(user_id, [])
    if not user_warnings or index >= len(user_warnings):
        await ctx.reply(embed=make_embed("경고 삭제 실패", "삭제할 경고 기록이 없어요.", RED), mention_author=False)
        return
    removed_warning = user_warnings.pop(index)
    if not user_warnings:
        config["warnings"].pop(user_id, None)
    if punishment_key and punishment_record:
        punishment_record["deleted_at"] = now_kst().isoformat()
        punishment_record["deleted_by_id"] = ctx.author.id
        punishment_record["deleted_by_name"] = str(ctx.author)

    save_config(ctx.guild.id, config)
    member = member or ctx.guild.get_member(int(user_id))
    removed_code = removed_warning.get("case_code") or removed_warning.get("case_id") or code
    after = len(config["warnings"].get(user_id, []))

    embed = warning_action_embed(
        ctx.guild,
        "경고 삭제",
        member,
        ctx.author,
        reason,
        f"삭제 1개 / 남은 경고 {after}회",
        str(removed_code),
    )
    await ctx.reply(embed=embed, mention_author=False)
    await send_log(ctx.guild, embed)


@bot.command(name="경고초기화", aliases=["clearwarns"])
@commands.check(can_use_moderation)
async def clear_warnings(ctx: commands.Context, target: str, *, reason: str | None = None) -> None:
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return
    if member.id == ctx.author.id:
        await ctx.reply(embed=make_embed("경고 초기화 실패", "자기 자신의 경고 기록은 초기화할 수 없어요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    removed = len(config["warnings"].get(str(member.id), []))
    if removed <= 0:
        await ctx.reply(embed=make_embed("경고 초기화 실패", f"{member.mention}님의 초기화할 경고 기록이 없어요.", RED), mention_author=False)
        return
    config["warnings"][str(member.id)] = []
    save_config(ctx.guild.id, config)

    embed = warning_action_embed(ctx.guild, "경고 초기화", member, ctx.author, reason, f"초기화 {removed}개")
    await ctx.reply(embed=embed, mention_author=False)
    await send_log(ctx.guild, embed)


@bot.command(name="뮤트", aliases=["mute", "타임아웃"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, target: str, duration: str | None = None, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "timeout"):
        return

    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    ok, message = can_target(ctx, member)
    if not ok:
        await ctx.reply(embed=make_embed("처벌 불가", message, RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    used_default_time = False
    duration_text = duration
    delta = parse_duration(duration_text) if duration_text else None
    if delta:
        duration_text = seconds_to_duration_text(int(delta.total_seconds()))
    if not duration_text:
        used_default_time = True
        default_seconds = int(config.get("default_timeout_seconds", 600))
        delta = dt.timedelta(seconds=default_seconds)
        duration_text = seconds_to_duration_text(default_seconds)
    elif not delta:
        used_default_time = True
        reason = f"{duration_text} {reason or ''}".strip()
        default_seconds = int(config.get("default_timeout_seconds", 600))
        delta = dt.timedelta(seconds=default_seconds)
        duration_text = seconds_to_duration_text(default_seconds)

    if not delta:
        await ctx.reply(embed=make_embed("시간 형식 오류", "예: `10분`, `2시간`, `1일`, `1주`, `하루`, `일주일`, `10m`, `2h`, `1w`", RED), mention_author=False)
        return
    now = dt.datetime.now(dt.timezone.utc)
    until = stacked_timeout_until(member, delta)
    max_until = now + dt.timedelta(days=28)
    if delta > dt.timedelta(days=28) or until > max_until:
        await ctx.reply(embed=make_embed("시간 초과", "디스코드 타임아웃은 현재 시점 기준 최대 28일까지 가능해요.", RED), mention_author=False)
        return

    max_seconds = max_timeout_for(ctx.author)
    requested_seconds = int(delta.total_seconds())
    if max_seconds is not None and requested_seconds > max_seconds:
        await ctx.reply(
            embed=make_embed(
                "타임아웃 한도 초과",
                f"현재 운영팀 역할로는 최대 `{seconds_to_duration_text(max_seconds)}`까지 가능해요.",
                RED,
            ),
            mention_author=False,
        )
        return

    await member.timeout(until, reason=short_reason(reason))
    duration_text = timeout_until_duration_text(until, now)
    action = f"뮤트 ({duration_text})"
    if used_default_time:
        action += " - 기본 시간"
    embed = await punish_log(ctx, action, member, reason, TIMEOUT)
    config = load_config(ctx.guild.id)
    timeout_case_id, timeout_record = latest_timeout_record(config, member.id)
    config["active_timeouts"][str(member.id)] = {
        "until": until.isoformat(),
        "target_name": str(member),
        "reason": short_reason(reason),
        "punishment_case_id": timeout_case_id,
        "log_channel_id": timeout_record.get("log_channel_id") if timeout_record else None,
        "log_message_id": timeout_record.get("log_message_id") if timeout_record else None,
    }
    save_config(ctx.guild.id, config)
    schedule_timeout_release(ctx.guild.id, member.id, until.isoformat())
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="언뮤트", aliases=["unmute"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, target: str, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "untimeout"):
        return

    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    now = dt.datetime.now(dt.timezone.utc)
    current_timeout = member.timed_out_until
    if not current_timeout or current_timeout.astimezone(dt.timezone.utc) <= now:
        config = load_config(ctx.guild.id)
        if config.get("active_timeouts", {}).pop(str(member.id), None):
            save_config(ctx.guild.id, config)
        cancel_timeout_release(ctx.guild.id, member.id)
        await ctx.reply(embed=make_embed("해제 불가", "해당 유저는 현재 타임아웃 상태가 아니에요.", RED), mention_author=False)
        return

    release_reason = short_reason(reason)
    key = (ctx.guild.id, member.id)
    config = load_config(ctx.guild.id)
    saved_timeout_data = config.get("active_timeouts", {}).get(str(member.id))
    PENDING_MANUAL_UNMUTES.add(key)
    cancel_timeout_release(ctx.guild.id, member.id)
    try:
        await member.timeout(None, reason=release_reason)
        config = load_config(ctx.guild.id)
        timeout_data = config["active_timeouts"].pop(str(member.id), None) or saved_timeout_data
        case_id = next_case_id(config)
        save_punishment_record(config, case_id, "언뮤트", member, ctx.author, release_reason)
        save_config(ctx.guild.id, config)

        embed = timeout_release_embed(member, ctx.author, release_reason, get_case_code(config, case_id))
        await reply_to_log(ctx.guild, timeout_data if isinstance(timeout_data, dict) else None, embed)
        await ctx.reply(embed=embed, mention_author=False)
    finally:
        bot.loop.create_task(clear_pending_manual_unmute(key))


@bot.command(name="킥", aliases=["kick"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx: commands.Context, target: str, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "kick"):
        return

    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    ok, message = can_target(ctx, member)
    if not ok:
        await ctx.reply(embed=make_embed("처벌 불가", message, RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "킥", member, ctx.author, reason)
    save_config(ctx.guild.id, config)
    embed = removal_embed(get_case_code(config, case_id), "추방", member, ctx.author, reason, KICK)
    await member.kick(reason=short_reason(reason))
    await send_log(ctx.guild, embed)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="밴", aliases=["ban"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx: commands.Context, target: str, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "ban"):
        return

    target_user = await resolve_ban_target(ctx, target)
    if not target_user:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID를 확인하지 못했어요.", RED), mention_author=False)
        return

    if isinstance(target_user, discord.Member):
        ok, message = can_target(ctx, target_user)
        if not ok:
            await ctx.reply(embed=make_embed("처벌 불가", message, RED), mention_author=False)
            return

    config = load_config(ctx.guild.id)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "밴", target_user, ctx.author, reason)
    save_config(ctx.guild.id, config)
    embed = ban_action_embed(ctx.guild, get_case_code(config, case_id), "밴", target_user, ctx.author, reason)
    await ctx.guild.ban(target_user, reason=short_reason(reason), delete_message_days=0)
    message = await send_log(ctx.guild, embed)
    config = load_config(ctx.guild.id)
    attach_log_message(config, case_id, message)
    save_config(ctx.guild.id, config)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="언밴", aliases=["unban"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "unban"):
        return

    user = discord.Object(id=user_id)
    config = load_config(ctx.guild.id)
    _, ban_record = latest_ban_record(config, user_id)
    await ctx.guild.unban(user, reason=short_reason(reason))
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "언밴", user, ctx.author, reason)
    save_config(ctx.guild.id, config)
    embed = ban_action_embed(ctx.guild, get_case_code(config, case_id), "언밴", user, ctx.author, reason)
    await reply_to_log(ctx.guild, ban_record, embed)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="청소", aliases=["clear", "purge"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, amount: int) -> None:
    if not await require_punishment_power(ctx, "purge"):
        return

    if amount < 1 or amount > 100:
        await ctx.reply(embed=make_embed("범위 오류", "1개부터 100개까지 삭제할 수 있어요.", RED), mention_author=False)
        return

    deleted = await ctx.channel.purge(limit=amount + 1)
    deleted_count = max(0, len(deleted) - 1)
    embed = make_embed("메시지 청소", f"`{deleted_count}`개의 메시지를 삭제했어요.", GREEN)
    await ctx.send(embed=embed, delete_after=5)


if __name__ == "__main__":
    load_env_file()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(".env 파일에 DISCORD_TOKEN을 설정해 주세요.")
    bot.run(token)
