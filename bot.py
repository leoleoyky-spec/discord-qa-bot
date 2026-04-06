"""
Discord Q&A Bot for noteメンバーシップ
- 質問をClaude APIでAUTO/REVIEWに振り分け
- AUTO: 自動返信 + スプレッドシート記録
- REVIEW: オーナーにDM通知 + ボタンで承認送信
"""

import os
import json
import base64
import datetime
import asyncio
import logging

import discord
from discord import ui
from discord.ext import commands
import anthropic
import httpx
import gspread
from google.oauth2.service_account import Credentials

# ── ログ設定 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 環境変数 ──
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OWNER_DISCORD_ID = int(os.environ["OWNER_DISCORD_ID"])
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "1484832551628439664"))
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1F-vZRvfMCgXulI8I8Cqn6eXj0EONwTFIJKUPpeIucls")

# ブログアウトプットチャンネル（自動リアクション）
BLOG_CHANNEL_ID = int(os.environ.get("BLOG_CHANNEL_ID", "1485086627490431066"))

# 入り口チャンネル（新メンバー歓迎）
ENTRANCE_CHANNEL_ID = int(os.environ.get("ENTRANCE_CHANNEL_ID", "1473853429418819749"))
WELCOME_STICKER_ID = int(os.environ.get("WELCOME_STICKER_ID", "1490522895183773876"))

# スタッフ判定キーワード
STAFF_KEYWORDS = ["ゆーぽん", "みお事務局", "みお"]

# スクショ要求トリガーキーワード
ERROR_KEYWORDS = ["エラー", "できない", "動かない", "開かない", "失敗", "おかしい"]

# スクショ要求メッセージ
SCREENSHOT_REQUEST_MSG = "スクショを送ってもらえますか？📸 画像を添付して返信してください！"

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


# ── Claude API ──
claude_client = None
if ANTHROPIC_API_KEY and ANTHROPIC_API_KEY != "PLACEHOLDER":
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
else:
    logger.warning("ANTHROPIC_API_KEY未設定: 質問振り分け機能は無効です")

CLASSIFY_PROMPT = """あなたはDiscordコミュニティの質問振り分けアシスタントです。
以下の質問を分析して、JSON形式で返してください。

## 振り分けルール
- **AUTO**: AIツールの操作方法、手順の質問、技術的なHow-to、簡単な確認質問
  例: 「ChatGPTの使い方」「Canvaの操作」「プロンプトの書き方」「〇〇の設定方法」
- **REVIEW**: ビジネス戦略の相談、クレーム・不満、複雑な個別相談、感情的な内容、金銭に関する相談
  例: 「売上が伸びない」「方向性に悩んでいる」「返金してほしい」「モチベーションが…」

## 出力形式（JSON のみ返してください）
{
  "classification": "AUTO" または "REVIEW",
  "confidence": 0.0〜1.0,
  "reason": "振り分け理由（日本語で短く）",
  "suggested_answer": "AUTOの場合の回答案（丁寧で親しみやすい口調。REVIEWの場合は空文字）"
}

## 質問:
"""

def classify_question(question_text: str) -> dict:
    if claude_client is None:
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": "Claude API未設定",
            "suggested_answer": "",
        }
    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": CLASSIFY_PROMPT + question_text}
            ],
        )
        text = response.content[0].text.strip()
        # JSON部分を抽出
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"Claude API エラー: {e}")
        return {
            "classification": "REVIEW",
            "confidence": 0.0,
            "reason": f"分類エラー: {e}",
            "suggested_answer": "",
        }


# ── スクショ解析（Vision API） ──
def analyze_screenshot(image_url: str, media_type: str) -> str:
    if claude_client is None:
        return ""
    try:
        image_data = base64.standard_b64encode(httpx.get(image_url).content).decode("utf-8")
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": "このスクショのエラー内容を読み取り、AIツールの使い方コミュニティのサポートとして解決策を100〜300文字で返してください。",
                    },
                ],
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Vision API エラー: {e}")
        return ""


# ── スプレッドシート記録 ──
def log_to_sheet(ws, author: str, question: str, classification: str, answer: str, status: str):
    if ws is None:
        logger.warning("スプレッドシート未接続のため記録スキップ")
        return
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([now, author, question, classification, answer, status])
        logger.info(f"スプレッドシートに記録: {author} / {classification}")
    except Exception as e:
        logger.error(f"スプレッドシート書き込みエラー: {e}")


# ── Discord Bot ──
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Google Sheets接続（グローバル）
gc = None
worksheet = None


def is_staff(member_name: str, display_name: str) -> bool:
    for kw in STAFF_KEYWORDS:
        if kw in member_name or kw in display_name:
            return True
    return False


# ── 承認ボタンUI ──
class ReviewView(ui.View):
    def __init__(self, original_message: discord.Message, suggested_answer: str, result: dict):
        super().__init__(timeout=86400)  # 24時間
        self.original_message = original_message
        self.suggested_answer = suggested_answer
        self.result = result

    @ui.button(label="✅ AI回答を送信", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await self.original_message.reply(self.suggested_answer)
            log_to_sheet(
                worksheet,
                str(self.original_message.author),
                self.original_message.content,
                "REVIEW→AUTO送信",
                self.suggested_answer,
                "承認済み",
            )
            await interaction.response.edit_message(content="✅ 回答を送信しました！", view=None)
        except Exception as e:
            await interaction.response.send_message(f"送信エラー: {e}", ephemeral=True)

    @ui.button(label="✏️ 自分で返信する", style=discord.ButtonStyle.blurple)
    async def manual(self, interaction: discord.Interaction, button: ui.Button):
        log_to_sheet(
            worksheet,
            str(self.original_message.author),
            self.original_message.content,
            "REVIEW",
            "",
            "手動対応",
        )
        await interaction.response.edit_message(
            content="📝 手動対応に切り替えました。Discordで直接返信してください。", view=None
        )

    @ui.button(label="🔄 AI回答を再生成", style=discord.ButtonStyle.gray)
    async def regenerate(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        new_result = classify_question(self.original_message.content)
        new_answer = new_result.get("suggested_answer", "")
        if not new_answer:
            new_answer = "(回答案を生成できませんでした)"
        self.suggested_answer = new_answer
        embed = discord.Embed(
            title="🔄 再生成された回答案",
            description=new_answer[:4000],
            color=0x3498DB,
        )
        await interaction.followup.edit_message(
            interaction.message.id,
            content=interaction.message.content,
            embed=embed,
            view=self,
        )

    @ui.button(label="❌ 無視", style=discord.ButtonStyle.red)
    async def ignore(self, interaction: discord.Interaction, button: ui.Button):
        log_to_sheet(
            worksheet,
            str(self.original_message.author),
            self.original_message.content,
            "REVIEW",
            "",
            "無視",
        )
        await interaction.response.edit_message(content="❌ この質問を無視しました。", view=None)


@bot.event
async def on_ready():
    global gc, worksheet
    gc = restore_google_credentials()
    worksheet = get_worksheet(gc)
    logger.info(f"✅ 起動: {bot.user} (ID: {bot.user.id})")
    logger.info(f"   監視チャンネル: {CHANNEL_ID}")
    logger.info(f"   オーナー: {OWNER_DISCORD_ID}")
    if worksheet:
        logger.info("   スプレッドシート: 接続済み")
    else:
        logger.warning("   スプレッドシート: 未接続")


@bot.event
async def on_member_join(member: discord.Member):
    """新メンバー入室時に「入り口」チャンネルでスタンプを送る"""
    try:
        channel = bot.get_channel(ENTRANCE_CHANNEL_ID)
        if channel:
            sticker = await bot.fetch_sticker(WELCOME_STICKER_ID)
            await channel.send(stickers=[sticker])
            logger.info(f"歓迎スタンプ送信: {member.display_name}")
    except Exception as e:
        logger.error(f"歓迎スタンプエラー: {e}")
        # スタンプが使えない場合は手を振る絵文字メッセージで代替
        try:
            channel = bot.get_channel(ENTRANCE_CHANNEL_ID)
            if channel:
                await channel.send(f"👋 ようこそ {member.mention} さん！")
        except Exception as e2:
            logger.error(f"歓迎メッセージエラー: {e2}")


@bot.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージは無視
    if message.author.bot:
        return

    # ── ブログアウトプットチャンネル: 自動リアクション ──
    if message.channel.id == BLOG_CHANNEL_ID:
        try:
            await message.add_reaction("🥰")
        except Exception as e:
            logger.error(f"リアクション追加エラー: {e}")
        await bot.process_commands(message)
        return

    # 監視チャンネル以外は無視
    if message.channel.id != CHANNEL_ID:
        return

    # スタッフの投稿は無視
    display_name = message.author.display_name if hasattr(message.author, "display_name") else ""
    if is_staff(message.author.name, display_name):
        logger.info(f"スタッフ投稿をスキップ: {message.author.name}")
        return

    content = message.content.strip()

    # ── スクショ解析: 返信に画像が添付されている場合 ──
    if message.reference and message.attachments:
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            if ref_msg.author == bot.user and "スクショを送ってもらえますか" in ref_msg.content:
                attachment = message.attachments[0]
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    logger.info(f"スクショ解析開始: {message.author.name}")
                    analysis = analyze_screenshot(attachment.url, attachment.content_type)
                    if analysis:
                        await message.reply(analysis)
                        log_to_sheet(worksheet, str(message.author), content, "AUTO（スクショ解析）", analysis, "自動返信済み")
                        logger.info(f"スクショ解析返信完了: {message.author.name}")
                    else:
                        await message.reply("画像の解析に失敗しました。もう一度スクショを送っていただけますか？")
                    await bot.process_commands(message)
                    return
        except Exception as e:
            logger.error(f"スクショ解析エラー: {e}")

    # 短すぎるメッセージ（挨拶等）はスキップ
    if len(content) < 5:
        return

    # ── スクショ要求: エラー系キーワード + 画像なし ──
    if any(kw in content for kw in ERROR_KEYWORDS) and not message.attachments:
        await message.reply(SCREENSHOT_REQUEST_MSG)
        log_to_sheet(worksheet, str(message.author), content, "スクショ待ち", SCREENSHOT_REQUEST_MSG, "スクショ要求済み")
        logger.info(f"スクショ要求送信: {message.author.name}")
        await bot.process_commands(message)
        return

    logger.info(f"新しい質問を検出: {message.author.name} - {content[:50]}...")

    # Claude APIで分類
    result = classify_question(content)
    classification = result.get("classification", "REVIEW")
    suggested_answer = result.get("suggested_answer", "")
    confidence = result.get("confidence", 0)
    reason = result.get("reason", "")

    logger.info(f"分類結果: {classification} (信頼度: {confidence}) - {reason}")

    if classification == "AUTO" and confidence >= 0.7 and suggested_answer:
        # ── AUTO: 自動返信 ──
        try:
            await message.reply(suggested_answer)
            log_to_sheet(worksheet, str(message.author), content, "AUTO", suggested_answer, "自動返信済み")
            logger.info(f"AUTO返信完了: {message.author.name}")
        except Exception as e:
            logger.error(f"AUTO返信エラー: {e}")
    else:
        # ── REVIEW: オーナーにDM通知 ──
        try:
            owner = await bot.fetch_user(OWNER_DISCORD_ID)
            embed = discord.Embed(
                title="📩 新しい質問（要確認）",
                color=0xE74C3C,
                timestamp=message.created_at,
            )
            embed.add_field(name="質問者", value=message.author.display_name, inline=True)
            embed.add_field(name="判定", value=f"{classification} (信頼度: {confidence})", inline=True)
            embed.add_field(name="理由", value=reason[:200], inline=False)
            embed.add_field(name="質問内容", value=content[:1000], inline=False)
            if suggested_answer:
                embed.add_field(name="💡 AI回答案", value=suggested_answer[:1000], inline=False)
            embed.add_field(
                name="🔗 元メッセージ",
                value=f"[メッセージを見る]({message.jump_url})",
                inline=False,
            )

            view = ReviewView(message, suggested_answer, result)
            await owner.send(embed=embed, view=view)
            log_to_sheet(worksheet, str(message.author), content, "REVIEW", suggested_answer, "確認待ち")
            logger.info(f"REVIEW通知送信: {message.author.name} → オーナーDM")
        except Exception as e:
            logger.error(f"DM通知エラー: {e}")

    await bot.process_commands(message)


# ── ヘルスチェックコマンド ──
@bot.command(name="status")
async def status_cmd(ctx):
    if ctx.author.id != OWNER_DISCORD_ID:
        return
    embed = discord.Embed(title="🤖 Bot ステータス", color=0x2ECC71)
    embed.add_field(name="状態", value="稼働中", inline=True)
    embed.add_field(name="スプレッドシート", value="接続済み" if worksheet else "未接続", inline=True)
    embed.add_field(name="監視チャンネル", value=str(CHANNEL_ID), inline=True)
    await ctx.send(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
