#!/usr/bin/env python3
"""
Telegram-бот — калькулятор налогов для ИП в Венгрии (2026).
Режимы: Стандартный EV, Átalányadó, KATA.
"""

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# === СТАТИСТИКА (PostgreSQL) ===
ADMIN_ID = 266424785

_db_conn = None

def _get_db():
    """Подключение к PostgreSQL (lazy)."""
    global _db_conn
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        return None
    if _db_conn is None or _db_conn.closed:
        import psycopg2
        _db_conn = psycopg2.connect(db_url)
        _db_conn.autocommit = True
        with _db_conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stats_events (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    return _db_conn


def track(user_id, username, event, detail=None):
    """Записать событие в БД."""
    if user_id == ADMIN_ID:
        return
    try:
        conn = _get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO stats_events (user_id, username, event, detail) "
                    "VALUES (%s, %s, %s, %s)",
                    (user_id, username or '', event, detail),
                )
    except Exception as e:
        logger.warning(f"Stats error: {e}")


# === СТАВКИ НАЛОГОВ ВЕНГРИИ 2026 ===
SZJA_RATE = 0.15
SZOCHO_RATE = 0.13
TB_RATE = 0.185
TOTAL_TAX_RATE = SZJA_RATE + SZOCHO_RATE + TB_RATE  # 0.465
MIN_WAGE = 322_800       # минималка Ft/мес
GUAR_WAGE = 373_200      # гарант. бермінімум Ft/мес (квалифиц. деятельность)
KATA_MONTHLY = 50_000    # Ft/мес
KATA_LIMIT = 18_000_000  # лимит оборота Ft/год
SZJA_EXEMPT = MIN_WAGE * 12 / 2  # 1 936 800 Ft/год — адомéнтеш (только Átalányadó)
ATALANY_LIMIT = MIN_WAGE * 12 * 10  # 38 736 000 Ft/год — лимит оборота Átalányadó
AFA_EXEMPT_LIMIT = 20_000_000  # порог alanyi mentesség (ÁFA) Ft/год
HIPA_RATE = 0.02  # Будапешт (макс. 2%)
HIPA_SAVOS = [     # (лимит оборота/год, фикс. база)
    (12_000_000, 2_500_000),
    (18_000_000, 6_000_000),
    (25_000_000, 8_500_000),
]

# Состояния ConversationHandler
TAX_REGIME, TAX_COST_RATIO, TAX_EXPENSES, TAX_WAGE_BASE, TAX_INPUT_MODE, TAX_NUMBER = range(6)


# === РАСЧЁТНЫЕ ФУНКЦИИ ===

def fmt(n):
    """1000000 → 1 000 000"""
    if n < 0:
        return f"-{abs(n):,.0f}".replace(",", " ")
    return f"{n:,.0f}".replace(",", " ")


def calc_taxes(regime, mode, amount, expense_pct=0, wage_base=None):
    """
    Расчёт налогов (месячные суммы).
    regime: 'standard' | 'atalany' | 'kata'
    mode:   'revenue' | 'net' | 'tax'
    wage_base: MIN_WAGE или GUAR_WAGE (база мин. взносов)
    """
    if regime == 'kata':
        return _calc_kata(mode, amount)

    if wage_base is None:
        wage_base = MIN_WAGE

    expense_ratio = expense_pct / 100
    min_szocho = wage_base * SZOCHO_RATE
    min_tb = wage_base * TB_RATE
    min_social = min_szocho + min_tb

    # Льгота SZJA для Átalányadó (месячная)
    exempt = SZJA_EXEMPT / 12 if regime == 'atalany' else 0

    if mode == 'revenue':
        revenue = amount
        expenses = revenue * expense_ratio
        profit = revenue - expenses

    elif mode == 'net':
        net = amount
        # Сначала пробуем: прибыль > льготы (SZJA платится на profit - exempt)
        # net = profit - (profit - exempt)*SZJA - szocho - tb
        # Без минимумов: net = profit - (profit-exempt)*0.15 - profit*0.13 - profit*0.185
        #   = profit*(1 - 0.13 - 0.185) - (profit-exempt)*0.15
        #   = profit*0.685 - profit*0.15 + exempt*0.15
        #   = profit*0.535 + exempt*0.15
        # profit = (net - exempt*0.15) / 0.535
        if exempt > 0:
            profit_try = (net - exempt * SZJA_RATE) / (1 - TOTAL_TAX_RATE)
            if profit_try >= wage_base and profit_try > exempt:
                profit = profit_try
            elif profit_try <= exempt:
                # Весь доход в пределах льготы — SZJA = 0
                # net = profit - szocho - tb = profit - max(profit*0.315, min_social)
                if net >= wage_base * (1 - SZOCHO_RATE - TB_RATE):
                    profit = net / (1 - SZOCHO_RATE - TB_RATE)
                else:
                    profit = net + min_social
            else:
                profit = (net + min_social - exempt * SZJA_RATE) / (1 - SZJA_RATE)
        else:
            threshold = wage_base * (1 - TOTAL_TAX_RATE)
            if net >= threshold:
                profit = net / (1 - TOTAL_TAX_RATE)
            else:
                profit = (net + min_social) / (1 - SZJA_RATE)
        revenue = profit / (1 - expense_ratio) if expense_ratio < 1 else profit
        expenses = revenue * expense_ratio

    elif mode == 'tax':
        tax = amount
        # tax = szja + szocho + tb
        # szja = max(profit - exempt, 0) * 0.15
        if exempt > 0:
            # Если tax покрывает полные взносы: profit > wage_base и profit > exempt
            # tax = (profit-exempt)*0.15 + profit*0.13 + profit*0.185
            #     = profit*0.465 - exempt*0.15
            profit_try = (tax + exempt * SZJA_RATE) / TOTAL_TAX_RATE
            if profit_try >= wage_base and profit_try > exempt:
                profit = profit_try
            elif tax > min_social:
                # Минимумы + частичный SZJA
                profit = (tax - min_social + exempt * SZJA_RATE) / SZJA_RATE
                if profit < 0:
                    profit = 0
            else:
                profit = 0
        else:
            threshold = wage_base * TOTAL_TAX_RATE
            if tax >= threshold:
                profit = tax / TOTAL_TAX_RATE
            elif tax > min_social:
                profit = (tax - min_social) / SZJA_RATE
            else:
                profit = 0
        revenue = profit / (1 - expense_ratio) if expense_ratio < 1 else profit
        expenses = revenue * expense_ratio

    # Для Átalányadó: доход до SZJA_EXEMPT/12 в месяц освобождён от SZJA
    szja_exempt_monthly = SZJA_EXEMPT / 12 if regime == 'atalany' else 0
    taxable_for_szja = max(profit - szja_exempt_monthly, 0)
    szja = taxable_for_szja * SZJA_RATE

    szocho = max(profit * SZOCHO_RATE, min_szocho)
    tb = max(profit * TB_RATE, min_tb)
    total_tax = szja + szocho + tb
    net_result = profit - total_tax

    return {
        'revenue': revenue, 'expenses': expenses, 'profit': profit,
        'szja': szja, 'szocho': szocho, 'tb': tb,
        'szja_exempt': szja_exempt_monthly,
        'total_tax': total_tax, 'net': net_result,
    }


def _calc_kata(mode, amount):
    """KATA: фикс. 50 000 Ft/мес + 40% сверх лимита."""
    kata = KATA_MONTHLY
    extra = 0

    if mode == 'revenue':
        revenue = amount
        if revenue * 12 > KATA_LIMIT:
            extra = (revenue * 12 - KATA_LIMIT) * 0.40 / 12
        total_tax = kata + extra
        net = revenue - total_tax

    elif mode == 'net':
        net = amount
        revenue = net + kata
        if revenue * 12 > KATA_LIMIT:
            revenue = (net + kata - KATA_LIMIT * 0.4 / 12) / 0.6
            extra = (revenue * 12 - KATA_LIMIT) * 0.40 / 12
        total_tax = kata + extra
        net = revenue - total_tax

    else:
        revenue = 0
        total_tax = kata
        net = 0

    return {
        'revenue': revenue, 'expenses': 0, 'profit': revenue,
        'szja': 0, 'szocho': 0, 'tb': 0,
        'kata': kata, 'extra_tax': extra,
        'total_tax': total_tax, 'net': net,
        'is_kata': True,
    }


def calc_hipa_yearly(revenue_yearly, profit_yearly):
    """HIPA: sávos до 25M, стандарт выше."""
    for limit, base in HIPA_SAVOS:
        if revenue_yearly <= limit:
            return base * HIPA_RATE
    # > 25M: база ≈ прибыль (упрощённо)
    return profit_yearly * HIPA_RATE


def format_tax_result(r, regime, expense_pct, mode, input_amount, wage_base=None):
    """Форматирование результатов."""
    if wage_base is None:
        wage_base = MIN_WAGE
    names = {'standard': 'Стандартный EV', 'atalany': 'Átalányadó', 'kata': 'KATA'}
    mode_names = {'revenue': 'оборота', 'net': 'чистой прибыли', 'tax': 'суммы налогов'}
    base_label = "гарант." if wage_base == GUAR_WAGE else "мін."

    msg = f"🧮 <b>{names[regime]}</b>\n"
    msg += f"Расчёт из {mode_names[mode]}: {fmt(input_amount)} Ft/мес\n"
    if regime != 'kata' and expense_pct > 0:
        msg += f"Расходы: {expense_pct}%\n"
    if regime != 'kata':
        msg += f"Мін. база: {fmt(wage_base)} Ft ({base_label})\n"
    msg += "\n"

    is_kata = r.get('is_kata', False)
    minimums = not is_kata and r['profit'] < wage_base

    # HIPA
    rev_yr = r['revenue'] * 12
    profit_yr = r['profit'] * 12
    hipa_yr = calc_hipa_yearly(rev_yr, profit_yr)
    hipa_mo = hipa_yr / 12
    is_savos = rev_yr <= 25_000_000
    hipa_label = "sávos" if is_savos else "станд."

    total_with_hipa = r['total_tax'] + hipa_mo
    net_with_hipa = r['net'] - hipa_mo

    # --- Месяц ---
    msg += "\U0001f4c5 <b>В месяц:</b>\n"
    msg += f"  Оборот (доход): <b>{fmt(r['revenue'])}</b> Ft\n"
    if r['expenses'] > 0:
        msg += f"  Расходы ({expense_pct}%): -{fmt(r['expenses'])} Ft\n"
        msg += f"  Налог. база: {fmt(r['profit'])} Ft\n"

    msg += f"\n  Итого налоги: <b>-{fmt(total_with_hipa)} Ft</b>\n"

    if is_kata:
        msg += f"  KATA: -{fmt(r['kata'])} Ft\n"
        if r.get('extra_tax', 0) > 0:
            msg += f"  Доп. налог 40%: -{fmt(r['extra_tax'])} Ft\n"
    else:
        szja_ex = r.get('szja_exempt', 0)
        if szja_ex > 0 and r['szja'] == 0:
            msg += f"  SZJA (15%): 0 Ft (льгота до {fmt(szja_ex)} Ft/мес)\n"
        elif szja_ex > 0:
            msg += f"  SZJA (15%): -{fmt(r['szja'])} Ft (льгота {fmt(szja_ex)} Ft/мес)\n"
        else:
            msg += f"  SZJA (15%): -{fmt(r['szja'])} Ft\n"
        sn = " \u26a1\u043c\u0438\u043d." if minimums else ""
        msg += f"  SZOCHO (13%){sn}: -{fmt(r['szocho'])} Ft\n"
        msg += f"  TB (18.5%){sn}: -{fmt(r['tb'])} Ft\n"
    msg += f"  HIPA (2%, {hipa_label}): -{fmt(hipa_mo)} Ft\n"

    msg += f"\n  Чистая прибыль: <b>{fmt(net_with_hipa)} Ft</b>\n"
    if r['revenue'] > 0:
        eff = total_with_hipa / r['revenue'] * 100
        msg += f"  Эфф. ставка: {eff:.1f}%\n"

    # --- Год ---
    msg += f"\n\U0001f4c5 <b>В год:</b>\n"
    msg += f"  Оборот: <b>{fmt(rev_yr)}</b> Ft\n"
    if r['expenses'] > 0:
        msg += f"  Расходы: -{fmt(r['expenses'] * 12)} Ft\n"
    msg += f"  Налоги (вкл. HIPA {fmt(hipa_yr)} Ft): <b>-{fmt(total_with_hipa * 12)} Ft</b>\n"
    msg += f"  Чистая: <b>{fmt(net_with_hipa * 12)} Ft</b>\n"

    # Предупреждения
    if net_with_hipa < 0:
        msg += "\n⚠️ Чистая прибыль отрицательная!\n"
    if minimums:
        msg += f"\n⚡ Минимальные взносы (база &lt; {fmt(wage_base)} Ft)\n"
    if is_kata and rev_yr > KATA_LIMIT:
        msg += f"\n⚠️ Превышен лимит KATA ({fmt(KATA_LIMIT)} Ft/год)\n"
    if regime == 'atalany' and rev_yr > ATALANY_LIMIT:
        msg += (f"\n⚠️ Оборот {fmt(rev_yr)} Ft/год превышает лимит "
                f"Átalányadó ({fmt(ATALANY_LIMIT)} Ft/год)!\n"
                "Необходимо перейти на стандартный EV.\n")
    if not is_kata and rev_yr > AFA_EXEMPT_LIMIT:
        msg += (f"\n⚠️ Оборот превышает {fmt(AFA_EXEMPT_LIMIT)} Ft/год — "
                "необходима регистрация плательщиком ÁFA (27%).\n"
                "Подробнее: /vat\n")
    elif not is_kata and rev_yr > 0:
        msg += (f"\n✅ Оборот в пределах {fmt(AFA_EXEMPT_LIMIT)} Ft/год — "
                "можно использовать освобождение от ÁFA (alanyi mentesség).\n")

    return msg


# === ОБРАБОТЧИКИ TELEGRAM ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    u = update.effective_user
    track(u.id, u.username, 'start')
    await update.message.reply_text(
        "🧮 <b>Калькулятор налогов ИП — Венгрия 2026</b>\n\n"
        "🏢 Бот создан командой Hungary Visa Shop\n"
        "📩 Для записи на консультацию по ВНЖ в Венгрии и режимам налогов для ИП пишите @HungaryVisaShop\n\n"
        "Команды:\n"
        "/tax — рассчитать налоги\n"
        "/regimes — режимы налогообложения\n"
        "/rates — текущие ставки\n"
        "/mrot — минималка и квалификация\n"
        "/vat — справочник ÁFA (НДС)\n"
        "/cancel — отменить расчёт\n\n"
        "Нажмите /tax чтобы начать.",
        parse_mode='HTML',
    )


async def show_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /rates — показать ставки"""
    min_total = MIN_WAGE * (SZOCHO_RATE + TB_RATE)
    guar_total = GUAR_WAGE * (SZOCHO_RATE + TB_RATE)
    sep = "━━━━━━━━━━━━━━━━━━━━━"

    await update.message.reply_text(
        "📊 <b>Ставки налогов 2026</b>\n\n"
        #
        f"{sep}\n"
        f"<b>SZJA — подоходный налог: {SZJA_RATE:.0%}</b>\n"
        "Считается от прибыли (доход − расходы).\n"
        "Для Átalányadó — от прибыли после вычета "
        "нормы расходов (45/80/90%).\n\n"
        #
        f"{sep}\n"
        f"<b>SZOCHO — соц. взнос: {SZOCHO_RATE:.0%}</b>\n"
        "Считается от той же базы, что и SZJA.\n"
        "Но не менее минималки (см. ниже).\n\n"
        #
        f"{sep}\n"
        f"<b>TB — соц. страхование: {TB_RATE:.1%}</b>\n"
        "Считается от той же базы, что и SZJA.\n"
        "Но не менее минималки (см. ниже).\n\n"
        #
        f"{sep}\n"
        f"<b>Итого SZJA + SZOCHO + TB: {TOTAL_TAX_RATE:.1%}</b>\n"
        "Применяется к прибыли. Если прибыль ниже "
        "минималки — взносы считаются от минималки.\n\n"
        #
        f"{sep}\n"
        "<b>Минимальная база (минималка)</b>\n"
        f"  {fmt(MIN_WAGE)} Ft/мес\n"
        f"  Мин. взносы SZOCHO+TB: {fmt(min_total)} Ft/мес\n"
        "Если прибыль за месяц ниже минималки, "
        "SZOCHO и TB всё равно платятся от неё.\n\n"
        #
        f"{sep}\n"
        "<b>Гарантированная минималка</b>\n"
        f"  {fmt(GUAR_WAGE)} Ft/мес\n"
        f"  Мин. взносы SZOCHO+TB: {fmt(guar_total)} Ft/мес\n"
        "Для квалифицированной деятельности "
        "(средне-спец. или высшее образование).\n\n"
        #
        f"{sep}\n"
        f"<b>KATA: {fmt(KATA_MONTHLY)} Ft/мес</b>\n"
        f"Фиксированный налог. Лимит дохода: {fmt(KATA_LIMIT)} Ft/год.\n"
        "Превышение лимита — доплата 40% с суммы сверх.\n\n"
        #
        f"{sep}\n"
        f"<b>HIPA — местный налог (Будапешт): {HIPA_RATE:.0%}</b>\n"
        "Считается от прибыли. В Будапеште — "
        "упрощённые пороги (sávos):\n"
        "  до 12M Ft → 50 000 Ft/год\n"
        "  12–18M Ft → 120 000 Ft/год\n"
        "  18–25M Ft → 170 000 Ft/год\n"
        "  &gt;25M Ft → прибыль × 2%\n"
        "В других городах ставка может отличаться.",
        parse_mode='HTML',
    )


async def show_regimes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /regimes — справочник по режимам"""
    await update.message.reply_text(
        "📋 <b>Режимы налогообложения ИП (2026)</b>\n\n"
        "<b>KATA</b> — упрощённый фикс. налог\n\n"
        "  Клиенты: только физлица и не связ. лица\n"
        "  Для кого: фрилансеры (beauty мастера, репетиторы, фотографы и т.д.)\n\n"
        f"  Налог: {fmt(KATA_MONTHLY)} Ft/мес (фикс.)\n"
        f"  Лимит: {fmt(KATA_LIMIT)} Ft/год\n"
        f"  Если оборот больше {fmt(KATA_LIMIT)} Ft/год —\n"
        "  налог 40% с суммы превышения\n\n"
        "  Преимущества:\n"
        "  Нет SZJA, SZOCHO, TB\n"
        "  Нет ÁFA (VAT)\n"
        "  Доступ к бесплатным гос. мед. услугам\n"
        "  Простые счета, не нужен бухгалтер на постоянной основе\n\n"
        "  Не подходит для IT-аутсорса на одну компанию\n"
        "  или работы с крупным заказчиком\n"
        "  Сотрудники: нельзя нанимать\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Átalányadó</b> — нормативные расходы\n\n"
        "  Клиенты: любые (🇭🇺 🇪🇺 🌍), без ограничений\n"
        "  Для кого: IT, консалтинг, услуги с малыми расходами\n\n"
        "  Налоги: SZJA 15% + SZOCHO 13% + TB 18.5%\n"
        "  Норма расходов: 45%, 80% или 90% (с 2027 — 50%)\n"
        f"  Льгота SZJA: первые {fmt(SZJA_EXEMPT)} Ft/год не облагаются\n"
        f"  Лимит: {fmt(ATALANY_LIMIT)} Ft/год\n"
        "  При превышении — переход на стандартный EV\n\n"
        "  Преимущества:\n"
        "  Самый популярный режим для IT-фрилансеров\n"
        "  Не нужно подтверждать расходы\n"
        "  Льгота SZJA экономит ~290 000 Ft/год\n"
        "  Работа с любыми клиентами по всему миру\n\n"
        "  Сотрудники: можно, но невыгодно (расходы не вычитаются)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Стандартный EV</b> — по факт. расходам\n\n"
        "  Клиенты: любые (🇭🇺 🇪🇺 🌍), без ограничений\n"
        "  Для кого: торговля, производство, большие обороты\n\n"
        "  Налоги: SZJA 15% + SZOCHO 13% + TB 18.5%\n"
        "  Расходы: фактические (подтверждённые документами)\n"
        "  Без лимита оборота\n"
        "  Без льготы SZJA\n\n"
        "  Преимущества:\n"
        "  Нет ограничения по обороту\n"
        "  Все расходы уменьшают налог. базу\n"
        "  Можно нанимать сотрудников (зарплаты = расходы)\n\n"
        "  Нужен бухгалтер и подтверждение расходов\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Все режимы: + HIPA (местный налог, Будапешт 2%)\n"
        "Подробнее: /rates — ставки, /vat — ÁFA",
        parse_mode='HTML',
    )


async def show_vat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /vat — справочник ÁFA"""
    await update.message.reply_text(
        "📋 <b>ÁFA (НДС) для ИП — 2026</b>\n\n"
        "<b>Ставки:</b>\n"
        "  27% — стандартная\n"
        "  18% — продукты питания, общепит\n"
        "  5% — книги, лекарства, жильё\n\n"
        f"<b>Порог освобождения (alanyi mentesség):</b>\n"
        f"  2026: {fmt(AFA_EXEMPT_LIMIT)} Ft/год\n"
        "  2027: 22 000 000 Ft/год\n"
        "  2028: 24 000 000 Ft/год\n\n"
        "Если оборот ≤ порога — можно не начислять ÁFA.\n"
        "Если превышен — обязательная регистрация.\n\n"
        "<b>Кому начисляется ÁFA (если вы плательщик):</b>\n"
        "  🇭🇺 Клиент в Венгрии → 27%\n"
        "  🇪🇺 ЕС, B2B (есть EU VAT ID) → 0% (reverse charge)\n"
        "  🇪🇺 ЕС, B2C → 27% (венгерский ÁFA)\n"
        "  🌍 Вне ЕС → 0%\n\n"
        "⚠️ KATA-плательщики автоматически освобождены от ÁFA.",
        parse_mode='HTML',
    )


async def show_mrot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /mrot — справочник по МРОТ"""
    min_total = MIN_WAGE * (SZOCHO_RATE + TB_RATE)
    guar_total = GUAR_WAGE * (SZOCHO_RATE + TB_RATE)
    sep = "━━━━━━━━━━━━━━━━━━━━━"

    await update.message.reply_text(
        "💰 <b>МРОТ (минимальная зарплата) — 2026</b>\n\n"
        "МРОТ определяет минимальную базу для расчёта "
        "взносов SZOCHO и TB. Даже если прибыль ниже — "
        "взносы платятся от МРОТ.\n\n"
        #
        f"{sep}\n"
        f"<b>Минималка: {fmt(MIN_WAGE)} Ft/мес</b>\n"
        f"Мин. взносы SZOCHO+TB: {fmt(min_total)} Ft/мес\n\n"
        "Применяется, если деятельность <b>не требует</b> "
        "квалификации (специального образования).\n\n"
        "Примеры: уборка, курьер, торговля, "
        "beauty-услуги без спец. диплома.\n\n"
        #
        f"{sep}\n"
        f"<b>Гарантированная минималка: {fmt(GUAR_WAGE)} Ft/мес</b>\n"
        f"Мин. взносы SZOCHO+TB: {fmt(guar_total)} Ft/мес\n\n"
        "Применяется, если деятельность <b>требует</b> "
        "средне-специального или высшего образования.\n\n"
        "Примеры: IT-разработка, дизайн, бухгалтерия, "
        "юридические услуги, медицина, инженерия.\n\n"
        #
        f"{sep}\n"
        "<b>Как это влияет на налоги?</b>\n\n"
        "Если ваша прибыль за месяц ниже МРОТ — "
        "взносы SZOCHO и TB всё равно считаются от МРОТ.\n"
        "SZJA считается от фактической прибыли (может быть 0).\n\n"
        "Выбор МРОТ влияет только на Átalányadó и "
        "Стандартный EV. Для KATA — не применяется.",
        parse_mode='HTML',
    )


async def tax_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tax — начало расчёта"""
    keyboard = [
        [InlineKeyboardButton("KATA", callback_data="tax_r:kata")],
        [InlineKeyboardButton("Átalányadó", callback_data="tax_r:atalany")],
        [InlineKeyboardButton("Стандартный EV", callback_data="tax_r:standard")],
    ]
    await update.message.reply_text(
        "🧮 <b>Калькулятор налогов ИП</b>\n\n"
        "Выберите режим:\n"
        "Не знаете какой? Посмотрите описание /regimes",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
    )
    return TAX_REGIME


async def tax_regime_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор режима"""
    query = update.callback_query
    await query.answer()
    regime = query.data.split(":")[1]
    context.user_data['tax_regime'] = regime

    if regime == 'standard':
        await query.edit_message_text(
            "📊 <b>Стандартный EV</b>\n"
            "SZJA 15% + SZOCHO 13% + TB 18.5% на прибыль\n\n"
            "Введите % расходов от оборота\n(0 — если расходов нет):",
            parse_mode='HTML',
        )
        return TAX_EXPENSES

    elif regime == 'atalany':
        keyboard = [[
            InlineKeyboardButton("45%", callback_data="tax_c:45"),
            InlineKeyboardButton("80%", callback_data="tax_c:80"),
            InlineKeyboardButton("90%", callback_data="tax_c:90"),
        ]]
        await query.edit_message_text(
            "📊 <b>Átalányadó</b>\n"
            "Налоги на (оборот − норма расходов)\n\n"
            "Выберите норму расходов:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML',
        )
        return TAX_COST_RATIO

    else:  # kata
        context.user_data['tax_expense_pct'] = 0
        keyboard = [
            [InlineKeyboardButton("Знаю оборот (выручку)", callback_data="tax_m:revenue")],
            [InlineKeyboardButton("Знаю чистую прибыль", callback_data="tax_m:net")],
        ]
        await query.edit_message_text(
            "📊 <b>KATA</b>\n"
            f"Фикс. налог: {fmt(KATA_MONTHLY)} Ft/мес\n"
            f"Лимит: {fmt(KATA_LIMIT)} Ft/год (сверх +40%)\n\n"
            "Что известно?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML',
        )
        return TAX_INPUT_MODE


def _wage_base_keyboard():
    """Кнопки выбора базы мин. взносов"""
    return [
        [InlineKeyboardButton(
            f"Да — МРОТ {fmt(GUAR_WAGE)} Ft",
            callback_data="tax_w:guar")],
        [InlineKeyboardButton(
            f"Нет — МРОТ {fmt(MIN_WAGE)} Ft",
            callback_data="tax_w:min")],
    ]


async def tax_cost_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Норма расходов (Átalányadó)"""
    query = update.callback_query
    await query.answer()
    ratio = int(query.data.split(":")[1])
    context.user_data['tax_expense_pct'] = ratio

    await query.edit_message_text(
        f"📊 <b>Átalányadó</b> (норма расходов {ratio}%)\n\n"
        "Деятельность требует квалификации?\n"
        "Не уверены? Смотрите /mrot",
        reply_markup=InlineKeyboardMarkup(_wage_base_keyboard()),
        parse_mode='HTML',
    )
    return TAX_WAGE_BASE


async def tax_expenses_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод % расходов (Стандартный EV)"""
    text = update.message.text.strip().replace('%', '').replace(',', '.').replace(' ', '')
    try:
        pct = float(text)
        if pct < 0 or pct >= 100:
            await update.message.reply_text("Введите число от 0 до 99:")
            return TAX_EXPENSES
    except ValueError:
        await update.message.reply_text("Введите число (например: 30):")
        return TAX_EXPENSES

    context.user_data['tax_expense_pct'] = pct

    await update.message.reply_text(
        f"📊 <b>Стандартный EV</b> (расходы {pct:.0f}%)\n\n"
        "Деятельность требует квалификации?\n"
        "Не уверены? Смотрите /mrot",
        reply_markup=InlineKeyboardMarkup(_wage_base_keyboard()),
        parse_mode='HTML',
    )
    return TAX_WAGE_BASE


async def tax_wage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор базы мин. взносов"""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    context.user_data['tax_wage_base'] = GUAR_WAGE if choice == 'guar' else MIN_WAGE

    keyboard = [
        [InlineKeyboardButton("Знаю оборот (выручку)", callback_data="tax_m:revenue")],
        [InlineKeyboardButton("Знаю чистую прибыль", callback_data="tax_m:net")],
        [InlineKeyboardButton("Знаю сумму налогов", callback_data="tax_m:tax")],
    ]
    await query.edit_message_text(
        "Что известно?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TAX_INPUT_MODE


async def tax_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор что вводим"""
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":")[1]
    context.user_data['tax_mode'] = mode

    prompts = {
        'revenue': '💰 Введите месячный оборот в Ft:',
        'net': '💰 Введите желаемую чистую прибыль в месяц (Ft):',
        'tax': '💰 Введите сумму налогов в месяц (Ft):',
    }
    await query.edit_message_text(prompts[mode])
    return TAX_NUMBER


async def tax_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод суммы → расчёт"""
    text = update.message.text.strip()
    text = text.lower().replace('ft', '').replace('huf', '').replace(' ', '').replace('\xa0', '').replace(',', '')

    try:
        amount = float(text)
        if amount <= 0:
            await update.message.reply_text("Введите положительное число:")
            return TAX_NUMBER
    except ValueError:
        await update.message.reply_text("Введите число (например: 1000000):")
        return TAX_NUMBER

    regime = context.user_data['tax_regime']
    mode = context.user_data['tax_mode']
    expense_pct = context.user_data.get('tax_expense_pct', 0)

    wage_base = context.user_data.get('tax_wage_base', MIN_WAGE)
    result = calc_taxes(regime, mode, amount, expense_pct, wage_base)

    # Итеративная корректировка на HIPA для обратных расчётов
    if mode in ('net', 'tax'):
        for _ in range(10):
            hipa_mo = calc_hipa_yearly(
                result['revenue'] * 12, result['profit'] * 12) / 12
            if mode == 'net':
                adj = amount + hipa_mo
            else:
                adj = max(amount - hipa_mo, 0)
            new_result = calc_taxes(regime, mode, adj, expense_pct, wage_base)
            if abs(new_result['revenue'] - result['revenue']) < 1:
                result = new_result
                break
            result = new_result

    msg = format_tax_result(result, regime, expense_pct, mode, amount, wage_base)

    u = update.effective_user
    track(u.id, u.username, 'calc', f'{regime}/{mode}/{amount}')

    await update.message.reply_text(msg, parse_mode='HTML')
    context.user_data.clear()
    return ConversationHandler.END


async def tax_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel"""
    await update.message.reply_text("❌ Расчёт отменён.")
    context.user_data.clear()
    return ConversationHandler.END


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — статистика (только для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return
    conn = _get_db()
    if not conn:
        await update.message.reply_text("БД не подключена.")
        return
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM stats_events WHERE event='start'")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM stats_events WHERE event='calc'")
        total_calcs = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM stats_events "
            "WHERE event='start' AND created_at > NOW() - INTERVAL '7 days'")
        week_users = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM stats_events "
            "WHERE event='calc' AND created_at > NOW() - INTERVAL '7 days'")
        week_calcs = cur.fetchone()[0]
        cur.execute(
            "SELECT detail, COUNT(*) FROM stats_events "
            "WHERE event='calc' GROUP BY detail ORDER BY COUNT(*) DESC LIMIT 5")
        top = cur.fetchall()
    msg = "📊 <b>Статистика бота</b>\n\n"
    msg += f"<b>Всего:</b>\n"
    msg += f"  Пользователей: {total_users}\n"
    msg += f"  Расчётов: {total_calcs}\n\n"
    msg += f"<b>За 7 дней:</b>\n"
    msg += f"  Новых пользователей: {week_users}\n"
    msg += f"  Расчётов: {week_calcs}\n"
    if top:
        msg += "\n<b>Популярные расчёты:</b>\n"
        for detail, cnt in top:
            parts = (detail or '').split('/')
            regime = parts[0] if parts else '?'
            msg += f"  {regime}: {cnt}\n"
    await update.message.reply_text(msg, parse_mode='HTML')


# === ЗАПУСК ===

def main():
    BOT_TOKEN = os.getenv('TAX_BOT_TOKEN')
    if not BOT_TOKEN:
        logger.error("TAX_BOT_TOKEN не найден в .env файле!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    tax_handler = ConversationHandler(
        entry_points=[CommandHandler('tax', tax_start)],
        states={
            TAX_REGIME: [CallbackQueryHandler(tax_regime_cb, pattern='^tax_r:')],
            TAX_COST_RATIO: [CallbackQueryHandler(tax_cost_cb, pattern='^tax_c:')],
            TAX_EXPENSES: [MessageHandler(filters.TEXT & ~filters.COMMAND, tax_expenses_input)],
            TAX_WAGE_BASE: [CallbackQueryHandler(tax_wage_cb, pattern='^tax_w:')],
            TAX_INPUT_MODE: [CallbackQueryHandler(tax_mode_cb, pattern='^tax_m:')],
            TAX_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, tax_number_input)],
        },
        fallbacks=[CommandHandler('cancel', tax_cancel)],
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('regimes', show_regimes))
    application.add_handler(CommandHandler('rates', show_rates))
    application.add_handler(CommandHandler('vat', show_vat))
    application.add_handler(CommandHandler('mrot', show_mrot))
    application.add_handler(CommandHandler('stats', show_stats))
    application.add_handler(tax_handler)

    logger.info("Tax bot запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
