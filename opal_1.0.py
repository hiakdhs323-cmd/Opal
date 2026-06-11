import asyncio
import datetime as dt
import json
import os
import re
import secrets
import string
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
RED = 0xE74C3C
DARK = 0x2B2D31
ORANGE = 0xF39C12
CARD = 0x34443E
KICK = 0xFF8F7A
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
    "edit": "📝",
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


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
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(normalize_config(config), ensure_ascii=False, indent=4), encoding="utf-8")
    os.replace(tmp_path, path)


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


def make_embed(title: str, description: str | None = None, color: int = BLUE) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=now_kst())
    embed.set_footer(text="Opal")
    return embed


def short_reason(reason: str | None) -> str:
    return (reason or "운영진 재량").strip()[:900]


def code_block_text(value: str | None) -> str:
    return short_reason(value).replace("```", "`\u200b``")


def seconds_to_duration_text(seconds: int) -> str:
    units = (
        ("일", 86400),
        ("시간", 3600),
        ("분", 60),
        ("초", 1),
    )
    for label, unit_seconds in units:
        if seconds % unit_seconds == 0 and seconds >= unit_seconds:
            return f"{seconds // unit_seconds}{label}"
    return f"{seconds}초"


def member_text(user: discord.abc.User) -> str:
    mention = getattr(user, "mention", str(user))
    return f"{mention} (`{user.id}`)"


def display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", str(user))


def case_id_text(case_id: int | str) -> str:
    return str(case_id).lstrip("#")


def action_name(action: str) -> str:
    if "뮤트" in action:
        return "타임아웃 처벌 진행중"
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
    for threshold in thresholds:
        if total <= threshold:
            return threshold
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
        title=f"{emoji} {case_id_text(case_id)}",
        color=color or CARD,
        timestamp=now_kst(),
    )
    duration = action_duration(action)
    embed.description = f"**{action_name(action)}**"
    embed.add_field(name="👥 대상자", value=user_mention(target), inline=True)
    if duration != "-":
        embed.add_field(name="🕒 지속시간", value=f"`{duration}`", inline=True)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.add_field(name="처리자", value=handler_text(moderator), inline=False)
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
        title=f"{emoji} {case_id_text(case_id)}",
        color=ORANGE,
        timestamp=now_kst(),
    )
    embed.description = "**경고 지급**"
    embed.add_field(name="👥 대상자", value=target.mention, inline=True)
    embed.add_field(name="☑️ 경고누적", value=f"**{total} / {warning_limit_for(target.guild, total)}**", inline=True)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.add_field(name="처리자", value=handler_text(moderator), inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    return embed


def timeout_release_embed(
    target: discord.Member,
    moderator: discord.abc.User | None,
    reason: str,
    case_code: str | None = None,
) -> discord.Embed:
    emoji = embed_emoji(target.guild, "untimeout")
    embed = discord.Embed(
        title=f"{emoji} {case_code or display_name(target)}",
        color=0x7CF0C6,
        timestamp=now_kst(),
    )
    embed.description = "**타임아웃 해제**"
    embed.add_field(name="👥 대상자", value=target.mention, inline=False)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.add_field(name="처리자", value=handler_text(moderator), inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    return embed


def removal_embed(
    case_id: int,
    action: str,
    target: discord.Member,
    moderator: discord.abc.User,
    reason: str | None,
    color: int,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    guild = guild or guild_from_user(target)
    emoji_key = "ban" if action == "차단" else "kick"
    emoji = embed_emoji(guild, emoji_key)
    embed = discord.Embed(
        title=f"{emoji} {case_id_text(case_id)}",
        color=color,
        timestamp=now_kst(),
    )
    embed.description = f"**{action} 처벌**"
    embed.add_field(name="👥 대상자", value=user_mention(target), inline=False)
    embed.add_field(name="💬 사유", value=f"```{code_block_text(reason)}```", inline=False)
    embed.add_field(name="처리자", value=handler_text(moderator), inline=False)
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
        title=f"{emoji} {case_id_text(code)}",
        color=BLUE,
        timestamp=now_kst(),
    )
    embed.description = "**처벌 기록 수정**"
    embed.add_field(name="👥 대상자", value=f"`{record.get('target_name', record.get('target_id'))}`", inline=True)
    embed.add_field(name="📌 처벌", value=f"`{record.get('action', '처벌')}`", inline=True)
    if duration_text:
        embed.add_field(name="🕒 지속시간", value=f"`{old_duration_text or '-'}` → `{duration_text}`", inline=False)
    embed.add_field(name="이전 사유", value=f"```{code_block_text(old_reason)}```", inline=False)
    embed.add_field(name="새 사유", value=f"```{code_block_text(record.get('reason'))}```", inline=False)
    embed.add_field(name="처리자", value=handler_text(moderator), inline=False)
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
    match = re.fullmatch(r"(\d+)\s*(초|분|시간|일|s|m|h|d)", value.lower())
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


@tasks.loop(seconds=30)
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

            member = guild.get_member(int(user_id)) if str(user_id).isdigit() else None
            if not member and str(user_id).isdigit():
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.DiscordException:
                    member = None

            config["active_timeouts"].pop(user_id, None)
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
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}도움말 | 처벌 관리"))
    print(f"로그인 완료: {bot.user} ({bot.user.id})")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    before_timeout = before.timed_out_until
    after_timeout = after.timed_out_until

    if before_timeout is None and after_timeout is not None:
        await asyncio.sleep(1)
        config = load_config(after.guild.id)
        if str(after.id) in config["active_timeouts"]:
            return

        entry = await fetch_recent_audit(after.guild, discord.AuditLogAction.member_update, after.id)
        moderator = entry.user if entry and entry.user else after.guild.me
        reason = entry.reason if entry and entry.reason else "운영진 재량"
        duration_seconds = max(1, int((after_timeout - dt.datetime.now(dt.timezone.utc)).total_seconds()))
        duration_text = seconds_to_duration_text(duration_seconds)
        action = f"뮤트 ({duration_text})"
        case_id = await log_external_punishment(after.guild, action, after, moderator, reason)
        config = load_config(after.guild.id)
        log_channel_id = None
        log_message_id = None
        config["active_timeouts"][str(after.id)] = {
            "until": after_timeout.isoformat(),
            "target_name": str(after),
            "reason": short_reason(reason),
        }
        if case_id:
            embed = punishment_embed("처벌 완료", get_case_code(config, case_id), action, after, moderator, reason, YELLOW)
            message = await send_log(after.guild, embed)
            attach_log_message(config, case_id, message)
            if message:
                log_channel_id = message.channel.id
                log_message_id = message.id
            config["active_timeouts"][str(after.id)]["punishment_case_id"] = case_id
            config["active_timeouts"][str(after.id)]["log_channel_id"] = log_channel_id
            config["active_timeouts"][str(after.id)]["log_message_id"] = log_message_id
        save_config(after.guild.id, config)
        return

    if before_timeout is None or after_timeout is not None:
        return

    await asyncio.sleep(1)
    config = load_config(after.guild.id)
    timeout_data = config["active_timeouts"].pop(str(after.id), None)
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
        embed = removal_embed(get_case_code(config, case_id), "차단", user, moderator, reason, RED, guild)
        await send_log(guild, embed)


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


@bot.command(name="도움말", aliases=["help", "명령어"])
async def help_command(ctx: commands.Context) -> None:
    embed = make_embed(
        "Opal 처벌봇 도움말",
        "멘션과 유저 ID를 모두 사용할 수 있어요. 예: `@유저`, `123456789012345678`",
        DARK,
    )
    embed.set_author(name=f"{ctx.guild.name} 관리 도구", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    embed.add_field(
        name="기본 설정",
        value=(
            f"`{PREFIX}도움말`\n"
            f"`{PREFIX}핑`\n"
            f"`{PREFIX}로그채널 #채널`\n"
            "`/로그채널지정`\n"
            "`/기본타임아웃지정`\n"
            "`/운영팀추가`\n"
            "`/운영팀처벌강도설정`"
        ),
        inline=False,
    )
    embed.add_field(
        name="경고 관리",
        value=(
            f"`{PREFIX}경고 @유저 [사유]`\n"
            f"`{PREFIX}경고 유저ID [사유]`\n"
            f"`{PREFIX}경고목록 @유저`\n"
            f"`{PREFIX}경고삭제 유저ID 번호`\n"
            f"`{PREFIX}경고초기화 @유저`\n"
            f"`{PREFIX}수정 처벌코드 새사유`\n"
            "사유 생략 시 `운영진 재량`"
        ),
        inline=False,
    )
    embed.add_field(
        name="처벌 관리",
        value=(
            f"`{PREFIX}뮤트 @유저 10분 [사유]`\n"
            f"`{PREFIX}뮤트 유저ID 10m [사유]`\n"
            f"`{PREFIX}언뮤트 @유저 [사유]`\n"
            f"`{PREFIX}킥 유저ID [사유]`\n"
            f"`{PREFIX}밴 @유저 [사유]`\n"
            f"`{PREFIX}언밴 유저ID [사유]`\n"
            f"`{PREFIX}청소 10`"
        ),
        inline=False,
    )
    embed.add_field(name="시간 예시", value="시간 생략 시 기본 `10분` 또는 서버 설정값을 사용해요.\n`30초`, `10분`, `2시간`, `1일` 또는 `30s`, `10m`, `2h`, `1d`", inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="핑", aliases=["ping"])
async def ping(ctx: commands.Context) -> None:
    await ctx.reply(embed=make_embed("퐁", f"지연 시간: `{round(bot.latency * 1000)}ms`", GREEN), mention_author=False)


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
        embed = make_embed("시간 형식 오류", "예: `10분`, `30분`, `2시간`, `1일`, `10m`, `2h`", RED)
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
    밴: str | None = None,
    언밴: str | None = None,
    경고: str | None = None,
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
        "ban": 밴,
        "unban": 언밴,
        "warn": 경고,
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
            f"{emojis['ban']} 밴\n"
            f"{emojis['unban']} 언밴\n"
            f"{emojis['warn']} 경고\n"
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
            embed = make_embed("시간 형식 오류", "예: `30분`, `2시간`, `1일`, `30m`, `2h`", RED)
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

    config = load_config(interaction.guild.id)
    user_warnings = config["warnings"].get(str(유저.id), [])
    embed = make_embed("경고 조회", f"{유저.mention}님의 경고 `{len(user_warnings)}`개", ORANGE)
    if user_warnings:
        lines = []
        for warning in user_warnings[-10:]:
            created_at = str(warning.get("created_at", ""))[:16].replace("T", " ")
            lines.append(f"`#{warning.get('case_id')}` {warning.get('reason', '운영진 재량')}\n`{created_at}`")
        embed.add_field(name="최근 기록", value="\n\n".join(lines), inline=False)
    else:
        embed.add_field(name="최근 기록", value="경고 기록이 없어요.", inline=False)
    embed.set_thumbnail(url=유저.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
            await interaction.response.send_message(embed=make_embed("시간 형식 오류", "예: `10분`, `30분`, `2시간`, `1일`", RED), ephemeral=True)
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
                until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
                await member.timeout(until, reason=reason)
                embed = await punish_log(ctx, f"뮤트 ({seconds_to_duration_text(seconds)})", member, reason, YELLOW)
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
                embed = removal_embed(get_case_code(config, case_id), "차단", member, ctx.author, reason, RED)
                await member.ban(reason=reason, delete_message_days=0)
                await send_log(ctx.guild, embed)
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

    lines = []
    for warning in user_warnings[-10:]:
        moderator = ctx.guild.get_member(int(warning.get("moderator_id", 0)))
        mod_text = moderator.mention if moderator else f"`{warning.get('moderator_id')}`"
        created_at = str(warning.get("created_at", ""))[:16].replace("T", " ")
        shown_code = warning.get("case_code") or warning.get("case_id")
        lines.append(f"`{shown_code}` {warning['reason']}\n담당자: {mod_text} | `{created_at}`")

    embed = make_embed("경고 목록", f"{member.mention}님의 최근 경고 {len(lines)}개 / 전체 {len(user_warnings)}개", ORANGE)
    embed.add_field(name="대상", value=member_text(member), inline=False)
    embed.add_field(name="최근 기록", value="\n\n".join(lines), inline=False)
    await ctx.reply(embed=embed, mention_author=False)


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
        record["action"] = update_action_duration(old_action, new_duration_text)
        member = ctx.guild.get_member(int(record.get("target_id", 0)))
        if member:
            until = dt.datetime.now(dt.timezone.utc) + new_duration
            await member.timeout(until, reason=record["reason"])
            active_config = load_config(ctx.guild.id)
            active_config["active_timeouts"][str(member.id)] = {
                "until": until.isoformat(),
                "target_name": str(member),
                "reason": record["reason"],
            }
            active_config["punishments"][clean_case_id] = record
            config = active_config

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


@bot.command(name="경고삭제", aliases=["delwarn"])
@commands.check(can_use_moderation)
async def delete_warning(ctx: commands.Context, target: str, case_id: int) -> None:
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    user_id = str(member.id)
    before = len(config["warnings"].get(user_id, []))
    config["warnings"][user_id] = [warn for warn in config["warnings"].get(user_id, []) if warn.get("case_id") != case_id]
    after = len(config["warnings"][user_id])

    if before == after:
        await ctx.reply(embed=make_embed("경고 삭제 실패", f"`#{case_id}` 경고를 찾지 못했어요.", RED), mention_author=False)
        return

    save_config(ctx.guild.id, config)
    embed = make_embed("경고 삭제 완료", color=GREEN)
    embed.add_field(name="대상", value=member_text(member), inline=False)
    embed.add_field(name="삭제한 경고", value=f"`#{case_id}`", inline=True)
    embed.add_field(name="남은 경고", value=f"`{after}`회", inline=True)
    embed.add_field(name="담당자", value=member_text(ctx.author), inline=False)
    await ctx.reply(embed=embed, mention_author=False)
    await send_log(ctx.guild, embed)


@bot.command(name="경고초기화", aliases=["clearwarns"])
@commands.check(can_use_moderation)
async def clear_warnings(ctx: commands.Context, target: str) -> None:
    member = await resolve_member(ctx, target)
    if not member:
        await ctx.reply(embed=make_embed("대상 오류", "멘션 또는 유저 ID로 서버 멤버를 찾지 못했어요.", RED), mention_author=False)
        return

    config = load_config(ctx.guild.id)
    removed = len(config["warnings"].get(str(member.id), []))
    config["warnings"][str(member.id)] = []
    save_config(ctx.guild.id, config)

    embed = make_embed("경고 초기화 완료", color=GREEN)
    embed.add_field(name="대상", value=member_text(member), inline=False)
    embed.add_field(name="초기화한 경고", value=f"`{removed}`개", inline=True)
    embed.add_field(name="담당자", value=member_text(ctx.author), inline=False)
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
        await ctx.reply(embed=make_embed("시간 형식 오류", "예: `10분`, `2시간`, `1일`, `10m`, `2h`, `1d`", RED), mention_author=False)
        return
    if delta > dt.timedelta(days=28):
        await ctx.reply(embed=make_embed("시간 초과", "디스코드 타임아웃은 최대 28일까지 가능해요.", RED), mention_author=False)
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

    until = dt.datetime.now(dt.timezone.utc) + delta
    await member.timeout(until, reason=short_reason(reason))
    action = f"뮤트 ({duration_text})"
    if used_default_time:
        action += " - 기본 시간"
    embed = await punish_log(ctx, action, member, reason, YELLOW)
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

    release_reason = short_reason(reason)
    await member.timeout(None, reason=release_reason)
    config = load_config(ctx.guild.id)
    timeout_data = config["active_timeouts"].pop(str(member.id), None)
    case_id = next_case_id(config)
    save_punishment_record(config, case_id, "언뮤트", member, ctx.author, release_reason)
    save_config(ctx.guild.id, config)

    embed = timeout_release_embed(member, ctx.author, release_reason, get_case_code(config, case_id))
    await reply_to_log(ctx.guild, timeout_data if isinstance(timeout_data, dict) else None, embed)
    await ctx.reply(embed=embed, mention_author=False)


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
    save_punishment_record(config, case_id, "밴", member, ctx.author, reason)
    save_config(ctx.guild.id, config)
    embed = removal_embed(get_case_code(config, case_id), "차단", member, ctx.author, reason, RED)
    await member.ban(reason=short_reason(reason), delete_message_days=0)
    await send_log(ctx.guild, embed)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="언밴", aliases=["unban"])
@commands.check(can_use_moderation)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str | None = None) -> None:
    if not await require_punishment_power(ctx, "unban"):
        return

    user = discord.Object(id=user_id)
    await ctx.guild.unban(user, reason=short_reason(reason))
    embed = await punish_log(ctx, "언밴", user, reason, GREEN)
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
    embed = make_embed("메시지 청소", f"`{len(deleted) - 1}`개의 메시지를 삭제했어요.", GREEN)
    message = await ctx.send(embed=embed)
    await message.delete(delay=5)
    await send_log(ctx.guild, embed)


if __name__ == "__main__":
    load_env_file()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(".env 파일에 DISCORD_TOKEN을 설정해 주세요.")
    bot.run(token)
