"""
Discord Q&A Bot for noteメンバーシップ v3
- 質問をGemini APIでAUTO/REVIEWに振り分け
- AUTO: 自動返信 + スプレッドシート記録
- REVIEW: オーナーにDM通知 + ボタンで送信
- 過去Q&Aを参考にして回答精度を向上
"""

import os
import json
import base64
import asyncio
import datetime
import logging

import discord
from discord import ui
from discord.ext import commands
import httpx
import gspread
from google.oauth2.service_account import Credentials

# ── ログ設定 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 環境変数 ──
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OWNER_DISCORD_ID = int(os.environ["OWNER_DISCORD_ID"])
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "1484832551628439664"))
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1F-vZRvfMCgXulI8I8Cqn6eXj0EONwTFIJKUPpeIucls")

# ブログアウトプットチャンネル（自動リアクション）
BLOG_CHANNEL_ID = int(os.environ.get("BLOG_CHANNEL_ID", "1485086627490431066"))

# 入り口チャンネル（新メンバー歓迎）
ENTRANCE_CHANNEL_ID = int(os.environ.get("ENTRANCE_CHANNEL_ID", "1473853429418819749"))
WELCOME_STICKER_ID = int(os.environ.get("WELCOME_STICKER_ID", "1490522895183773876"))

# スタッフ判定（スタッフのユーザーIDを追加する場合はここに）
# オーナーにも返答するため、オーナーIDは含めない
STAFF_USER_IDS = []

# スクショ要求トリガーキーワード
ERROR_KEYWORDS = ["エラー", "できない", "動かない", "開かない", "失敗", "おかしい"]

SCREENSHOT_REQUEST_MSG = "スクショを送ってもらえますか？📸 画像を添付して返信してください！"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# ── Google Credentials復元 ──
def restore_google_credentials():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    if creds_b64:
        decoded = base64.b64decode(creds_b64)
        with open(creds_file, "wb") as f:
            f.write(decoded)
        logger.info("credentials.json をbase64から復元しました")
    elif not os.path.exists(creds_file):
        logger.warning("Google認証情報が見つかりません。スプレッドシート機能は無効です。")
        return None
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc


def get_worksheet(gc):
    if gc is None:
        return None
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet("Q&A記録")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="Q&A記録", rows=1000, cols=10)
            ws.append_row(["日時", "質問者", "質問内容", "判定", "回答", "ステータス"])
        return ws
    except Exception as e:
        logger.error(f"スプレッドシート接続エラー: {e}")
        return None


# ── 過去Q&Aキャッシュ ──
_qa_cache = []
_qa_cache_time = None
QA_CACHE_TTL = 300  # 5分間キャッシュ


def fetch_past_qas(ws) -> list:
    """スプレッドシートから過去のQ&Aを取得（キャッシュ付き）"""
    global _qa_cache, _qa_cache_time
    now = datetime.datetime.now()
    if _qa_cache_time and (now - _qa_cache_time).total_seconds() < QA_CACHE_TTL:
        return _qa_cache
    if ws is None:
        return []
    try:
        rows = ws.get_all_values()
        qas = []
        for row in rows[1:]:  # ヘッダー行をスキップ
            if len(row) >= 5 and row[2] and row[4]:  # 質問と回答がある行のみ
                qas.append({"question": row[2], "answer": row[4]})
        _qa_cache = qas
        _qa_cache_time = now
        logger.info(f"過去Q&Aキャッシュ更新: {len(qas)}件")
        return qas
    except Exception as e:
        logger.error(f"過去Q&A取得エラー: {e}")
        return _qa_cache  # エラー時は古いキャッシュを返す


def build_qa_reference(past_qas: list, max_entries: int = 10) -> str:
    """過去のQ&Aから参考情報を構築"""
    if not past_qas:
        return ""
    # 最新のエントリを優先（最大max_entries件）
    recent = past_qas[-max_entries:]
    lines = []
    for i, qa in enumerate(recent, 1):
        q = qa["question"][:200]
        a = qa["answer"][:300]
        lines.append(f"Q{i}: {q}\nA{i}: {a}")
    return "\n\n".join(lines)


# ── Gemini API ──
CLASSIFY_PROMPT_TEMPLATE = """あなたはDiscordコミュニティの質問振り分けアシスタント「みお」です。
AIツールの使い方コミュニティで、メンバーの質問に答えます。
以下の質問を分析して、JSON形式で返してください。

## 振り分けルール（重要：迷ったらAUTOにしてください）

### AUTO（自動回答）— ほとんどの質問はこちら
- AIツール（Claude、ChatGPT、Gemini、Canva、Manus等）の操作方法・使い方
- 技術的なHow-to、手順の質問、設定方法
- 「〇〇と△△どちらがいい？」「どれを使えばいい？」などの選択・比較の質問
- スプレッドシート、CSV、ファイル操作に関する質問
- プロンプトの書き方、コツ
- エラーや不具合の対処法
- 料金プランや機能の違いについての質問
- 「〇〇できますか？」「〇〇のやり方は？」系の質問すべて
  例: 「スキルを作りたい」「チャットとコワークの違いは？」「スプレッドシートがズレる」

### REVIEW（オーナー確認）— 以下の場合のみ
- オーナー個人の考え方・経験・意見を求める質問（「ミオさんはどう思いますか？」）
- クレーム・不満・返金要求
- 感情的な相談（モチベーション低下、悩み相談）
- 金銭に関する具体的な相談
- コミュニティの運営に関わる質問
  例: 「返金してほしい」「ミオさんの考えを聞きたい」「辛くて続けられない」

{qa_reference}

## 出力形式（JSON のみ返してください）
{{
  "classification": "AUTO" または "REVIEW",
  "confidence": 0.0〜1.0,
  "reason": "振り分け理由（日本語で短く）",
  "suggested_answer": "AUTOの場合の回答案（丁寧で親しみやすい口調、絵文字も少し使って。過去の回答例があれば参考にしつつ、質問に合わせてカスタマイズ。REVIEWの場合は空文字）"
}}

## 質問:
"""


async def call_gemini_async(prompt: str) -> str:
    """Gemini APIを非同期で呼び出してテキストを返す"""
    if not GEMINI_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini API エラー: {e}")
        return ""


async def call_gemini_with_image_async(prompt: str, image_data: str, media_type: str) -> str:
    """Gemini APIに画像付きで非同期リクエスト"""
    if not GEMINI_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json={
                    "contents": [{
                        "parts": [
                            {"inline_data": {"mime_type": media_type, "data": image_data}},
                            {"text": prompt},
                        ]
                    }]
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini Vision API エラー: {e}")
        return ""


# 同期版（ReviewViewのボタンコールバックで使用）
def call_gemini(prompt: str) -> str:
    """Gemini APIを呼び出してテキストを返す（同期版・ボタン用）"""
    if not GEMINI_API_KEY:
        return ""
    try:
        resp = httpx.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini API エラー: {e}")
        return ""


async def classify_question_async(question_text: str, ws=None) -> dict:
    """質問を分類（非同期、過去Q&A参照付き）"""
    if not GEMINI_API_KEY:
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "AI API未設定",
            "suggested_answer": "",
        }

    # 過去のQ&Aを取得して参考情報を構築
    past_qas = await asyncio.to_thread(fetch_past_qas, ws)
    qa_ref = build_qa_reference(past_qas)
    if qa_ref:
        qa_section = f"## 過去の回答例（参考にしてください）\n{qa_ref}\n"
    else:
        qa_section = ""

    prompt = CLASSIFY_PROMPT_TEMPLATE.format(qa_reference=qa_section) + question_text
    text = await call_gemini_async(prompt)
    if not text:
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "API応答なし",
            "suggested_answer": "",
        }
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON解析エラー: {e} / text={text[:200]}")
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "解析エラー",
            "suggested_answer": "",
        }


def classify_question(question_text: str) -> dict:
    """質問を分類（同期版・ボタン再生成用）"""
    if not GEMINI_API_KEY:
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "AI API未設定",
            "suggested_answer": "",
        }
    # 同期版はキャッシュ済みのQ&Aを使用
    qa_ref = build_qa_reference(_qa_cache)
    if qa_ref:
        qa_section = f"## 過去の回答例（参考にしてください）\n{qa_ref}\n"
    else:
        qa_section = ""
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(qa_reference=qa_section) + question_text
    text = call_gemini(prompt)
    if not text:
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "API応答なし",
            "suggested_answer": "",
        }
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON解析エラー: {e} / text={text[:200]}")
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "解析エラー",
            "suggested_answer": "",
        }


async def analyze_screenshot_async(image_url: str, media_type: str) -> str:
    """スクリーンショットを非同期で解析"""
    if not GEMINI_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            image_resp = await client.get(image_url)
            image_data = base64.standard_b64encode(image_resp.content).decode("utf-8")
        return await call_gemini_with_image_async(
            "このスクショのエラー内容を読み取り、AIツールの使い方コミュニティのサポートとして解決策を100〜300文字で返してください。",
            image_data,
            media_type,
        )
    except Exception as e:
        logger.error(f"スクショ解析エラー: {e}")
        return ""


# ── スプレッドシート記録 ──
def _log_to_sheet_sync(ws, author: str, question: str, classification: str, answer: str, status: str):
    """同期版スプレッドシート書き込み"""
    if ws is None:
        logger.warning("スプレッドシート未接続のため記録スキップ")
        return
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([now, author, question, classification, answer, status])
        logger.info(f"スプレッドシートに記録: {author} / {classification}")
    except Exception as e:
        logger.error(f"スプレッドシート書き込みエラー: {e}")


async def log_to_sheet_async(ws, author: str, question: str, classification: str, answer: str, status: str):
    """非同期版スプレッドシート書き込み（イベントループをブロックしない）"""
    await asyncio.to_thread(_log_to_sheet_sync, ws, author, question, classification, answer, status)


# 同期版エイリアス（ReviewViewボタン用）
def log_to_sheet(ws, author: str, question: str, classification: str, answer: str, status: str):
    _log_to_sheet_sync(ws, author, question, classification, answer, status)


# ── Discord Bot ──
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

gc = None
worksheet = None


def is_staff(user_id: int) -> bool:
    return user_id in STAFF_USER_IDS


# ── 承認ボタンUI ──
class ReviewView(ui.View):
    def __init__(self, original_message: discord.Message, suggested_answer: str, result: dict):
        super().__init__(timeout=86400)
        self.original_message = original_message
        self.suggested_answer = suggested_answer
        self.result = result

    @ui.button(label="✅ AI回答を送信", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        try:
            thread = await self.original_message.create_thread(name=f"{self.original_message.author.display_name}さんの質問")
            await thread.send(self.suggested_answer)
            log_to_sheet(worksheet, str(self.original_message.author), self.original_message.content, "REVIEW→AUTO送信", self.suggested_answer, "承認済み")
            await interaction.response.edit_message(content="✅ 回答を送信しました！", view=None)
        except Exception as e:
            await interaction.response.send_message(f"送信エラー: {e}", ephemeral=True)

    @ui.button(label="✏️ 自分で返信する", style=discord.ButtonStyle.blurple)
    async def manual(self, interaction: discord.Interaction, button: ui.Button):
        log_to_sheet(worksheet, str(self.original_message.author), self.original_message.content, "REVIEW", "", "手動対応")
        await interaction.response.edit_message(content="📝 手動対応に切り替えました。Discordで直接返信してください。", view=None)

    @ui.button(label="🔄 AI回答を再生成", style=discord.ButtonStyle.gray)
    async def regenerate(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        new_result = classify_question(self.original_message.content)
        new_answer = new_result.get("suggested_answer", "") or "(回答案を生成できませんでした)"
        self.suggested_answer = new_answer
        embed = discord.Embed(title="🔄 再生成された回答案", description=new_answer[:4000], color=0x3498DB)
        await interaction.followup.edit_message(interaction.message.id, content=interaction.message.content, embed=embed, view=self)

    @ui.button(label="❌ 無視", style=discord.ButtonStyle.red)
    async def ignore(self, interaction: discord.Interaction, button: ui.Button):
        log_to_sheet(worksheet, str(self.original_message.author), self.original_message.content, "REVIEW", "", "無視")
        await interaction.response.edit_message(content="❌ この質問を無視しました。", view=None)


@bot.event
async def on_ready():
    global gc, worksheet
    gc = restore_google_credentials()
    worksheet = get_worksheet(gc)
    logger.info(f"✅ 起動: {bot.user} (ID: {bot.user.id})")
    logger.info(f"   監視チャンネル: {CHANNEL_ID}")
    logger.info(f"   ブログチャンネル: {BLOG_CHANNEL_ID}")
    logger.info(f"   オーナー: {OWNER_DISCORD_ID}")
    logger.info(f"   Gemini API: {'有効' if GEMINI_API_KEY else '無効'}")
    logger.info(f"   スプレッドシート: {'接続済み' if worksheet else '未接続'}")
    # 過去Q&Aキャッシュを初期化
    if worksheet:
        past_qas = fetch_past_qas(worksheet)
        logger.info(f"   過去Q&A: {len(past_qas)}件読み込み済み")
    # チャンネル権限確認
    for guild in bot.guilds:
        logger.info(f"   サーバー: {guild.name} (ID: {guild.id})")
        target_ch = guild.get_channel(CHANNEL_ID)
        if target_ch:
            perms = target_ch.permissions_for(guild.me)
            logger.info(f"   → 監視チャンネル: {target_ch.name} 読み取り={perms.read_messages} 送信={perms.send_messages} スレッド={perms.create_public_threads}")
        else:
            logger.warning(f"   → 監視チャンネル: 見つからない！(ID: {CHANNEL_ID})")


@bot.event
async def on_member_join(member: discord.Member):
    try:
        channel = bot.get_channel(ENTRANCE_CHANNEL_ID)
        if channel:
            sticker = await bot.fetch_sticker(WELCOME_STICKER_ID)
            await channel.send(stickers=[sticker])
            logger.info(f"歓迎スタンプ送信: {member.display_name}")
    except Exception as e:
        logger.error(f"歓迎スタンプエラー: {e}")
        try:
            channel = bot.get_channel(ENTRANCE_CHANNEL_ID)
            if channel:
                await channel.send(f"👋 ようこそ {member.mention} さん！")
        except Exception as e2:
            logger.error(f"歓迎メッセージエラー: {e2}")


@bot.event
async def on_message(message: discord.Message):
    logger.info(f"メッセージ受信: ch={message.channel.id} author={message.author.name} content={message.content[:30]}")

    if message.author.bot:
        return

    # ── ブログアウトプットチャンネル: 自動リアクション ──
    if message.channel.id == BLOG_CHANNEL_ID:
        try:
            await message.add_reaction("🥰")
            logger.info(f"🥰リアクション追加: {message.author.name}")
        except Exception as e:
            logger.error(f"リアクション追加エラー: {e}")
        await bot.process_commands(message)
        return

    # 監視チャンネル以外は無視
    if message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return

    # スタッフの投稿は無視
    if is_staff(message.author.id):
        logger.info(f"スタッフ投稿をスキップ: {message.author.name}")
        await bot.process_commands(message)
        return

    content = message.content.strip()

    # ── スクショ解析 ──
    if message.reference and message.attachments:
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            if ref_msg.author == bot.user and "スクショを送ってもらえますか" in ref_msg.content:
                attachment = message.attachments[0]
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    logger.info(f"スクショ解析開始: {message.author.name}")
                    analysis = await analyze_screenshot_async(attachment.url, attachment.content_type)
                    if analysis:
                        await message.reply(analysis)
                        await log_to_sheet_async(worksheet, str(message.author), content, "AUTO（スクショ解析）", analysis, "自動返信済み")
                    else:
                        await message.reply("画像の解析に失敗しました。もう一度スクショを送っていただけますか？")
                    await bot.process_commands(message)
                    return
        except Exception as e:
            logger.error(f"スクショ解析エラー: {e}")

    if len(content) < 5:
        await bot.process_commands(message)
        return

    # ── スクショ要求 ──
    if any(kw in content for kw in ERROR_KEYWORDS) and not message.attachments:
        try:
            thread = await message.create_thread(name=f"{message.author.display_name}さんの質問")
            await thread.send(SCREENSHOT_REQUEST_MSG)
            await log_to_sheet_async(worksheet, str(message.author), content, "スクショ待ち", SCREENSHOT_REQUEST_MSG, "スクショ要求済み")
            logger.info(f"スクショ要求送信: {message.author.name}")
        except Exception as e:
            logger.error(f"スクショ要求エラー: {e}")
        await bot.process_commands(message)
        return

    logger.info(f"新しい質問を検出: {message.author.name} - {content[:50]}...")

    # 非同期で質問を分類（過去Q&A参照付き）
    result = await classify_question_async(content, worksheet)
    classification = result.get("classification", "REVIEW")
    suggested_answer = result.get("suggested_answer", "")
    confidence = result.get("confidence", 0)
    reason = result.get("reason", "")

    logger.info(f"分類結果: {classification} (信頼度: {confidence}) - {reason}")

    if classification == "AUTO" and confidence >= 0.5 and suggested_answer:
        try:
            thread = await message.create_thread(name=f"{message.author.display_name}さんの質問")
            await thread.send(suggested_answer)
            await log_to_sheet_async(worksheet, str(message.author), content, "AUTO", suggested_answer, "自動返信済み")
            logger.info(f"AUTO返信完了（スレッド）: {message.author.name}")
        except Exception as e:
            logger.error(f"AUTO返信エラー: {e}")
    else:
        try:
            owner = await bot.fetch_user(OWNER_DISCORD_ID)
            embed = discord.Embed(title="📩 新しい質問（要確認）", color=0xE74C3C, timestamp=message.created_at)
            embed.add_field(name="質問者", value=message.author.display_name, inline=True)
            embed.add_field(name="判定", value=f"{classification} (信頼度: {confidence})", inline=True)
            embed.add_field(name="理由", value=reason[:200], inline=False)
            embed.add_field(name="質問内容", value=content[:1000], inline=False)
            if suggested_answer:
                embed.add_field(name="💡 AI回答案", value=suggested_answer[:1000], inline=False)
            embed.add_field(name="🔗 元メッセージ", value=f"[メッセージを見る]({message.jump_url})", inline=False)
            view = ReviewView(message, suggested_answer, result)
            await owner.send(embed=embed, view=view)
            await log_to_sheet_async(worksheet, str(message.author), content, "REVIEW", suggested_answer, "確認待ち")
            logger.info(f"REVIEW通知送信: {message.author.name} → オーナーDM")
        except Exception as e:
            logger.error(f"DM通知エラー: {e}")

    await bot.process_commands(message)


@bot.command(name="status")
async def status_cmd(ctx):
    if ctx.author.id != OWNER_DISCORD_ID:
        return
    embed = discord.Embed(title="🤖 Bot ステータス", color=0x2ECC71)
    embed.add_field(name="状態", value="稼働中", inline=True)
    embed.add_field(name="AI", value="Gemini" if GEMINI_API_KEY else "無効", inline=True)
    embed.add_field(name="スプレッドシート", value="接続済み" if worksheet else "未接続", inline=True)
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
