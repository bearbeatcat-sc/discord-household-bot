import os
import sqlite3
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv
from google import genai

load_dotenv()
TOKEN = str(os.getenv('DISCORD_BOT_TOKEN'))
GEMINI_API_KEY = str(os.getenv('GEMINI_API_KEY'))

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

DB_PATH = 'payments.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paid_at TEXT NOT NULL,      -- ISO形式日時
            shop TEXT NOT NULL,
            amount INTEGER NOT NULL,
            category TEXT NOT NULL,
            payer TEXT NOT NULL,
            card_type TEXT NOT NULL,
            memo TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

def get_month_summary_for_ai():
    """Gemini に渡すための今月サマリを作る。
    ここでは:
      - 今月の合計
      - カテゴリ別
      - カード別
    をまとめて返す。
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 今月の合計
    cur.execute(
        """
        SELECT IFNULL(SUM(amount), 0)
        FROM payments
        WHERE strftime('%Y-%m', paid_at) = strftime('%Y-%m', 'now', 'localtime')
        AND is_deleted = 0
        """
    )
    total_row = cur.fetchone()
    total_amount = total_row[0] if total_row and total_row[0] is not None else 0

    # カテゴリ別
    cur.execute(
        """
        SELECT category, IFNULL(SUM(amount), 0) AS total
        FROM payments
        WHERE strftime('%Y-%m', paid_at) = strftime('%Y-%m', 'now', 'localtime')
        AND is_deleted = 0
        GROUP BY category
        ORDER BY total DESC
        """
    )
    category_rows = cur.fetchall()
    category_summary = [
        {"category": category, "amount": total}
        for category, total in category_rows
    ]
    # カード別
    cur.execute(
        """
        SELECT card_type, IFNULL(SUM(amount), 0) AS total
        FROM payments
        WHERE strftime('%Y-%m', paid_at) = strftime('%Y-%m', 'now', 'localtime')
        AND is_deleted = 0
        GROUP BY card_type
        ORDER BY total DESC
        """
    )
    card_rows = cur.fetchall()
    card_summary = [
        {"card_type": card_type, "amount": total}
        for card_type, total in card_rows
    ]

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
    ・親しみやすい口調で、堅苦しくならないようにしてください。]
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
    paid_at = datetime.now().isoformat(timespec="seconds")

    # とりあえず固定で "イオン"
    memo = ""  # まだ未使用なので空文字にしておく

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments (paid_at, shop, amount, category, payer, card_type, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (paid_at, shop, amount, category, payer, card_type, memo),
    )
    conn.commit()
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

@bot.command(name='recent')
async def recent(ctx, limit: int = 10):
    if limit <= 0:
        limit = 10
    if limit > 50:
        limit = 50

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, paid_at, shop, amount, category, payer, card_type
        FROM payments
        WHERE is_deleted = 0
        ORDER BY datetime(paid_at) DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await ctx.send("最近の支出記録はありません。")
        return
    
    lines = [f"直近 {len(rows)} 件の支出:"]
    for row in rows:
        pid, paid_at, shop, amount, category, payer, card_type = row
        lines.append(
            f"[ID: {pid}] {paid_at} {shop} {amount}円 "
            f"({category}, {payer}, カード: {card_type})"
        )

    await ctx.send("\n".join(lines))    

@bot.command(name='delete')
async def delete_payment(ctx, payment_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, paid_at, shop, amount, category, payer, card_type, is_deleted
        FROM payments
        WHERE id = ?
        """,
        (payment_id,),
    )
    row = cur.fetchone()

    if row is None:
        conn.close()
        await ctx.send(f"ID {payment_id} の支出記録は見つかりませんでした。")
        return
    
    pid, paid_at, shop, amount, category, payer, card_type, is_deleted = row

    if is_deleted:
        conn.close()
        await ctx.send(f"ID {payment_id} の支出記録は既に削除されています。")
        return
    
    cur.execute(
        "UPDATE payments SET is_deleted = 1 WHERE id = ?",
        (payment_id,),
    )
    conn.commit()
    conn.close()

    await ctx.send(f"ID {payment_id} の支出記録を削除しました：{paid_at
        } {shop} {amount}円 ({category}, {payer}, カード: {card_type})"
    )

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

    latest_payment = {"type:" "monthly_summary_request"}
    comment = ask_gemini_for_comment(latest_payment, month_summary)
    if comment:
        lines.append("")
        lines.append(f"AIコメント：{comment}")  

    await ctx.send("\n".join(lines))

@bot.command(name='month')
async def month(ctx):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT SUM(amount) 
        FROM payments
        WHERE strftime('%Y-%m', paid_at) = strftime('%Y-%m', 'now', 'localtime')
        AND is_deleted = 0
        """
    )
    row = cur.fetchone()
    conn.close()

    total = row[0] if row and row[0] is not None else 0
    await ctx.send(f"今月の合計支出は {total} 円です。")

if __name__ == '__main__':
    init_db()
    bot.run(TOKEN)
