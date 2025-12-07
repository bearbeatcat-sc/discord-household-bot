import os    
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from google import genai


load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

# 必須環境変数のバリデーション
missing_vars = []
if not TOKEN:
    missing_vars.append('DISCORD_BOT_TOKEN')
if not GEMINI_API_KEY:
    missing_vars.append('GEMINI_API_KEY')
if not DATABASE_URL:
    missing_vars.append('DATABASE_URL')
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

DB_PATH = 'payments.db'

def get_db_conn():
    if DATABASE_URL is None:
        raise ValueError("DATABASE_URL is not set in environment variables.")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)
    return conn

def init_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    paid_at TIMESTAMPTZ NOT NULL,
                    shop TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    payer TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    memo TEXT,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE
                );
                """
            )
        conn.commit()
    finally:
        conn.close()

def get_month_summary_for_ai():
    """Gemini に渡すための今月サマリを作る。
    ここでは:
      - 今月の合計
      - カテゴリ別
      - カード別
    をまとめて返す。
    """
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # 今月の合計
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM payments
                WHERE date_trunc('month', paid_at) = date_trunc('month', now())
                  AND is_deleted = FALSE
                """
            )
            total_row = cur.fetchone()
            total_amount = total_row[0] if total_row and total_row[0] is not None else 0

            # カテゴリ別
            cur.execute(
                """
                SELECT category, COALESCE(SUM(amount), 0) AS total
                FROM payments
                WHERE date_trunc('month', paid_at) = date_trunc('month', now())
                  AND is_deleted = FALSE
                GROUP BY category
                ORDER BY total DESC
                """
            )
            category_rows = cur.fetchall()
            category_summary = [
                {"category": row[0], "amount": row[1]}
                for row in category_rows
            ]

            # カード別
            cur.execute(
                """
                SELECT card_type, COALESCE(SUM(amount), 0) AS total
                FROM payments
                WHERE date_trunc('month', paid_at) = date_trunc('month', now())
                  AND is_deleted = FALSE
                GROUP BY card_type
                ORDER BY total DESC
                """
            )
            card_rows = cur.fetchall()
            card_summary = [
                {"card_type": row[0], "amount": row[1]}
                for row in card_rows
            ]
    finally:
        conn.close()

    conn.close()

    return {
        "total_amount": total_amount,
        "category_summary": category_summary,
        "card_summary": card_summary,
    }   

def ask_gemini_for_comment(latest_payment, month_summary) -> str:
    """Gemini に今月の支出についてコメントをもらう。"""
    if GEMINI_API_KEY is None:
        return "Gemini APIキーが設定されていません。"
    
    prompt = f"""
    あなたは日本語で家計をゆるく見守るアシスタントです。

    ・出力は必ず日本語で、120文字以内で書いてください。
    ・親しみやすい口調で、堅苦しくならないようにしてください。
    ・絵文字は使わないでください
    ・敬語で話してください。

    [最新の支出]
    {latest_payment}

    [今月のサマリ]
    {month_summary}
    """

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = getattr(response, "text", "").strip()
        return text
    except Exception as e:
        # 失敗してもBot全体が死なないようにしておく
        print(f"[Gemini Error] {e}")
        return ""
    

@bot.event
async def on_ready():
    user = bot.user
    if user is None:
        print("Logged in: bot.user is None")
    else:
        print(f"Logged in as {user} (ID: {user.id})")
    print("------")

@bot.command(name="pay")
async def pay(ctx, amount: int, category: str, payer: str, card_type: str, *, shop: str):
    """
    使い方:
      !pay 3500 食費 共同 イオン イオンザビッグ
      !pay 1200 外食 夫 三井住友 マクドナルド

    引数:
      amount    : 金額（整数）
      category  : カテゴリ（食費 / 日用品 / 外食 など）
      payer     : 支払者（夫 / 妻 / 共同 など）
      card_type : カード種別（イオン / 三井住友 / エポス など好きな文字列）
      shop      : 店名（スペースを含めてもOK。最後の引数が全部ここに入る）
    """
    paid_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # とりあえず固定で "イオン"
    memo = ""  # まだ未使用なので空文字にしておく

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (paid_at, shop, amount, category, payer, card_type, memo)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (paid_at, shop, amount, category, payer, card_type, memo),
            )
        conn.commit()
    finally:
        conn.close()

    latest_payment = {
        "paid_at": paid_at,
        "shop": shop,
        "amount": amount,
        "category": category,
        "payer": payer,
        "card_type": card_type,
    }
    month_summary = get_month_summary_for_ai()
    comment = ask_gemini_for_comment(latest_payment, month_summary)

    # Discordへの返信本文を組み立て
    base_msg = (
        f"記録しました：{paid_at} {shop} {amount}円"
        f"（カテゴリ: {category}, 支払者: {payer}, カード: {card_type}）"
    )
    if comment:
        base_msg += f"\nAIコメント：{comment}"

    await ctx.send(base_msg)

@bot.command(name="recent")
async def recent(ctx, limit: int = 10):
    if limit <= 0:
        limit = 10
    if limit > 50:
        limit = 50

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, paid_at, shop, amount, category, payer, card_type
                FROM payments
                WHERE is_deleted = FALSE
                ORDER BY paid_at DESC
                LIMIT %s
                """,
                (limit,)
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        await ctx.send("表示できる支出がありません。")
        return

    lines = [f"直近 {len(rows)} 件の支出:"]

    for row in rows:
        pid = row[0]
        paid_at = row[1]
        shop = row[2]
        amount = row[3]
        category = row[4]
        payer = row[5]
        card_type = row[6]

        lines.append(
            f"[ID: {pid}] {paid_at} {shop} {amount}円 "
            f"({category}, {payer}, カード: {card_type})"
        )

    await ctx.send("\n".join(lines))

@bot.command(name="delete")
async def delete_payment(ctx, payment_id: int):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, paid_at, shop, amount, category, payer, card_type, is_deleted
                FROM payments
                WHERE id = %s
                """,
                (payment_id,),
            )
            row = cur.fetchone()

            if row is None:
                await ctx.send(f"ID {payment_id} の支出は見つかりませんでした。")
                return

            if row[7]:
                await ctx.send(f"ID {payment_id} の支出はすでに削除済みです。")
                return

            cur.execute(
                "UPDATE payments SET is_deleted = TRUE WHERE id = %s",
                (payment_id,),
            )
        conn.commit()
        await ctx.send(
            f"次の支出を削除扱いにしました：\n"
            f"[ID: {row[0]}] {row[1]} {row[2]} {row[3]}円 "
            f"({row[4]}, {row[5]}, カード: {row[6]})"
        )
    finally:
        conn.close()

@bot.command(name="undo")
async def undo(ctx):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, paid_at, shop, amount, category, payer, card_type
                FROM payments
                WHERE is_deleted = FALSE
                ORDER BY paid_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()

            if row is None:
                await ctx.send("取り消せる支出がありません。")
                return

            pid = row[0]

            cur.execute(
                "UPDATE payments SET is_deleted = TRUE WHERE id = %s",
                (pid,),
            )
        conn.commit()
        await ctx.send(
            f"直近の支出を取り消しました：\n"
            f"[ID: {row[0]}] {row[1]} {row[2]} {row[3]}円 "
            f"({row[4]}, {row[5]}, カード: {row[6]})"
        )    
    finally:
        conn.close()

@bot.command(name='summary')
async def summary(ctx):
    month_summary = get_month_summary_for_ai()
    total_amount = month_summary["total_amount"]
    category_summary = month_summary["category_summary"]
    card_summary = month_summary["card_summary"]

    now = datetime.now()
    year_month_label = now.strftime("%Y年%m月")

    lines = [f"{year_month_label}の支出サマリ："]
    lines.append(f"合計支出: {total_amount} 円")
    
    # カテゴリ別
    if category_summary:
        lines.append("")
        lines.append("カテゴリ別支出:")
        for item in category_summary:
            lines.append(f" - {item['category']}: {item['amount']:,} 円")

    # カード別
    if card_summary:
        lines.append("")
        lines.append("カード別支出:")
        for item in card_summary:
            lines.append(f" - {item['card_type']}: {item['amount']:,} 円")

    latest_payment = {"type": "monthly_summary_request"}
    comment = ask_gemini_for_comment(latest_payment, month_summary)
    if comment:
        lines.append("")
        lines.append(f"AIコメント：{comment}")  

    await ctx.send("\n".join(lines))

@bot.command(name='month')
async def month(ctx):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM payments
                WHERE date_trunc('month', paid_at) = date_trunc('month', now())
                  AND is_deleted = FALSE
                """
            )
            row = cur.fetchone()
        total = row[0] if row and row[0] is not None else 0
        await ctx.send(f"今月の合計支出は {total} 円です。")
    finally:
        conn.close()

if __name__ == '__main__':
    init_db()
    bot.run(TOKEN)
