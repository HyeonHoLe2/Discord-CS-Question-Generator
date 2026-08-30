import os
import json
import random
import re
from pathlib import Path
from datetime import datetime
import urllib.parse

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google import genai

load_dotenv()

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

REPO_PATH = str(Path(__file__).parent / "data")
SETTINGS_FILE = str(Path(__file__).parent / "settings.json")

CATEGORIES = {
    "알고리즘": "Algorithm",
    "자료구조": "Computer Science/Data Structure",
    "데이터베이스": "Computer Science/Database",
    "네트워크": "Computer Science/Network",
    "운영체제": "Computer Science/Operating System",
    "소프트웨어공학": "Computer Science/Software Engineering",
    "컴퓨터구조": "Computer Science/Computer Architecture",
    "디자인패턴": "Design Pattern",
    "언어": "Language",
    "웹": "Web",
    "리눅스": "Linux",
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def get_md_files(categories):
    files = []
    for cat in categories:
        folder = CATEGORIES.get(cat)
        if not folder:
            continue
        path = Path(REPO_PATH) / folder
        if path.exists():
            for md in path.rglob("*.md"):
                if md.name != "README.md":
                    files.append(md)
    return files


def clean_md_content(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
    content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
    content = re.sub(r'<br\s*/?>', '\n', content)
    content = re.sub(r'<[^>]+>', '', content)
    content = re.sub(r'\n{3,}', '\n\n', content).strip()

    if len(content) > 4000:
        content = content[:4000]
    return content


async def generate_question_with_gemini(filepath):
    topic = filepath.stem
    content = clean_md_content(filepath)

    prompt = (
        f"다음은 '{topic}' 주제에 관한 CS 면접 학습 자료입니다.\n\n"
        f"---\n{content}\n---\n\n"
        "이 자료를 바탕으로 실제 면접에서 나올 법한 질문 1개와 풀이 방향을 암시하는 짧은 힌트 1개를 한국어로 작성해주세요.\n"
        "힌트는 답을 직접 알려주지 말고 생각의 실마리만 제공해야 합니다.\n\n"
        "다음 형식으로만 작성해주세요 (다른 설명 없이):\n"
        "**질문**: (면접 질문)\n\n"
        "**힌트**: (답을 직접 언급하지 않는 짧은 힌트)"
    )

    response = await gemini_client.aio.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
    )
    return response.text


async def make_question_embed(filepath):
    topic = filepath.stem
    parent = filepath.parent.name

    try:
        description = await generate_question_with_gemini(filepath)
    except Exception as e:
        print(f"Claude API 오류 ({topic}): {e}")
        content = clean_md_content(filepath)
        content = re.sub(r'^#{4,6}\s+(.+)$', r'### \1', content, flags=re.MULTILINE)
        if len(content) > 2000:
            content = content[:2000] + "\n\n*... 내용이 길어 생략됐어요*"
        description = content

    embed = discord.Embed(
        title=f"📚 {topic}",
        description=description,
        color=0x5865F2,
    )
    embed.set_footer(text=f"📂 {parent}")
    return embed


GITHUB_BASE = "https://github.com/gyoogle/tech-interview-for-developer/blob/master"


def get_github_url(filepath):
    relative = filepath.relative_to(Path(REPO_PATH))
    encoded = urllib.parse.quote(str(relative).replace("\\", "/"))
    return f"{GITHUB_BASE}/{encoded}"


def find_md_file_by_topic(topic):
    for folder in CATEGORIES.values():
        path = Path(REPO_PATH) / folder
        if path.exists():
            for md in path.rglob("*.md"):
                if md.stem == topic and md.name != "README.md":
                    return md
    return None


async def evaluate_answer_with_gemini(topic, md_content, user_answer):
    prompt = (
        f"주제: {topic}\n\n"
        f"다음은 '{topic}'에 관한 원본 학습 자료입니다:\n\n{md_content}\n\n"
        f"지원자 답변: {user_answer}\n\n"
        "위 학습 자료를 근거로 지원자의 답변을 평가해주세요.\n\n"
        "다음 형식으로만 작성해주세요:\n"
        "**유사도**: X% (학습 자료의 핵심 내용 반영 비율 기준)\n\n"
        "**잘한 점**: (답변에서 언급된 핵심 내용)\n\n"
        "**보완할 점**: (놓친 핵심 내용)\n\n"
        "**모범 답안**: (학습 자료 기반 간결한 모범 답변)"
    )
    response = await gemini_client.aio.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
    )
    return response.text


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_message(message):
    if message.author.bot or not message.reference:
        return

    ref = message.reference.resolved
    if not ref or ref.author != bot.user or not ref.embeds:
        return

    embed = ref.embeds[0]
    topic = embed.title.replace("📚 ", "") if embed.title else None
    user_answer = message.content
    if not topic or not user_answer:
        return

    md_file = find_md_file_by_topic(topic)
    if not md_file:
        await message.reply("원본 문서를 찾을 수 없어요.")
        return

    md_content = clean_md_content(md_file)

    github_url = get_github_url(md_file)

    async with message.channel.typing():
        try:
            evaluation = await evaluate_answer_with_gemini(topic, md_content, user_answer)
        except Exception as e:
            print(f"채점 오류: {e}")
            evaluation = "채점 중 오류가 발생했어요. 잠시 후 다시 시도해주세요."

    await message.reply(f"{evaluation}\n\n📄 **원본 자료**: {github_url}")


@bot.event
async def on_ready():
    print(f"접속된 서버 수: {len(bot.guilds)}")
    for guild in bot.guilds:
        print(f"서버: {guild.name} ({guild.id})")
    try:
        synced = await bot.tree.sync()
        print(f"전역 동기화 완료: {len(synced)}개 명령어")
    except Exception as e:
        print(f"동기화 오류: {e}")
    daily_question.start()
    print(f"✅ 봇 온라인: {bot.user}")


@tasks.loop(minutes=1)
async def daily_question():
    now = datetime.now()
    settings = load_settings()

    for guild_id, gs in settings.items():
        send_time = gs.get("send_time", "09:00")
        try:
            hour, minute = map(int, send_time.split(":"))
        except ValueError:
            continue

        if now.hour != hour or now.minute != minute:
            continue

        channel_id = gs.get("channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(int(channel_id))
        if not channel:
            continue

        categories = gs.get("categories", list(CATEGORIES.keys()))
        daily_count = gs.get("daily_count", 3)
        files = get_md_files(categories)
        if not files:
            continue

        selected = random.sample(files, min(daily_count, len(files)))

        await channel.send(f"🌅 **오늘의 면접 준비** ({now.strftime('%Y-%m-%d')})")
        for fp in selected:
            embed = await make_question_embed(fp)
            await channel.send(embed=embed)


# ── /문제 ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="문제", description="랜덤 면접 문제를 가져옵니다")
@app_commands.describe(category="카테고리 (비워두면 설정된 카테고리 사용)")
@app_commands.choices(category=[
    app_commands.Choice(name=k, value=k) for k in CATEGORIES
])
async def get_question(interaction: discord.Interaction, category: str = None):
    settings = load_settings()
    gs = settings.get(str(interaction.guild_id), {})
    cats = [category] if category else gs.get("categories", list(CATEGORIES.keys()))

    files = get_md_files(cats)
    if not files:
        await interaction.response.send_message("문제를 찾을 수 없어요.", ephemeral=True)
        return

    await interaction.response.defer()
    embed = await make_question_embed(random.choice(files))
    await interaction.followup.send(embed=embed)


# ── /설정 ──────────────────────────────────────────────────────────────────────

settings_group = app_commands.Group(name="설정", description="봇 설정")


@settings_group.command(name="채널", description="매일 문제를 받을 채널을 설정합니다")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = load_settings()
    gid = str(interaction.guild_id)
    settings.setdefault(gid, {})["channel_id"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ 채널이 {channel.mention}으로 설정됐어요.")


@settings_group.command(name="개수", description="하루에 받을 문제 개수를 설정합니다 (1~10)")
@app_commands.describe(count="문제 개수")
async def set_count(interaction: discord.Interaction, count: int):
    if not 1 <= count <= 10:
        await interaction.response.send_message("1~10 사이로 입력해주세요.", ephemeral=True)
        return
    settings = load_settings()
    gid = str(interaction.guild_id)
    settings.setdefault(gid, {})["daily_count"] = count
    save_settings(settings)
    await interaction.response.send_message(f"✅ 하루 문제 개수가 **{count}개**로 설정됐어요.")


@settings_group.command(name="시간", description="문제를 받을 시간을 설정합니다 (예: 09:00)")
@app_commands.describe(time="시간 (HH:MM)")
async def set_time(interaction: discord.Interaction, time: str):
    try:
        h, m = map(int, time.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        await interaction.response.send_message("올바른 형식으로 입력해주세요. (예: 09:00)", ephemeral=True)
        return
    settings = load_settings()
    gid = str(interaction.guild_id)
    settings.setdefault(gid, {})["send_time"] = time
    save_settings(settings)
    await interaction.response.send_message(f"✅ 매일 **{time}**에 문제를 보내드릴게요.")


@settings_group.command(name="카테고리", description="받고 싶은 문제 카테고리를 설정합니다")
async def set_category(interaction: discord.Interaction):
    settings = load_settings()
    gid = str(interaction.guild_id)
    current = settings.get(gid, {}).get("categories", list(CATEGORIES.keys()))

    options = [
        discord.SelectOption(label=cat, value=cat, default=(cat in current))
        for cat in CATEGORIES
    ]

    class CategorySelect(discord.ui.Select):
        def __init__(self):
            super().__init__(
                placeholder="카테고리를 선택하세요 (복수 선택 가능)",
                min_values=1,
                max_values=len(options),
                options=options,
            )

        async def callback(self, i: discord.Interaction):
            s = load_settings()
            s.setdefault(str(i.guild_id), {})["categories"] = self.values
            save_settings(s)
            await i.response.send_message(f"✅ 카테고리: **{', '.join(self.values)}**")

    class CategoryView(discord.ui.View):
        def __init__(self):
            super().__init__()
            self.add_item(CategorySelect())

    await interaction.response.send_message("카테고리를 선택하세요:", view=CategoryView(), ephemeral=True)


@settings_group.command(name="확인", description="현재 설정을 확인합니다")
async def check_settings(interaction: discord.Interaction):
    settings = load_settings()
    gs = settings.get(str(interaction.guild_id), {})

    channel_id = gs.get("channel_id")
    channel = f"<#{channel_id}>" if channel_id else "미설정"
    count = gs.get("daily_count", 3)
    send_time = gs.get("send_time", "09:00")
    cats = gs.get("categories", list(CATEGORIES.keys()))

    embed = discord.Embed(title="⚙️ 현재 설정", color=0x57F287)
    embed.add_field(name="채널", value=channel, inline=True)
    embed.add_field(name="하루 문제 수", value=f"{count}개", inline=True)
    embed.add_field(name="전송 시간", value=send_time, inline=True)
    embed.add_field(name="카테고리", value=", ".join(cats), inline=False)
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(settings_group)
bot.run(os.getenv("DISCORD_TOKEN"))
