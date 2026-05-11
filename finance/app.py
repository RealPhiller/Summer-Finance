"""Summer Finance Tracker — Thai-language personal expense/income tracker."""

import os
import re
import json
import base64
from datetime import date, timedelta
from collections import Counter

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

try:
    from supabase import create_client
    _SUPABASE_OK = True
except ImportError:
    _SUPABASE_OK = False

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Finance Tracker",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Constants ─────────────────────────────────────────────────────────────────

CATEGORIES = [
    "Food & Drinks", "Transport", "Shopping", "Bills & Utilities",
    "Healthcare", "Entertainment", "Education", "Travel",
    "Income", "Transfer", "Other",
]

PAYMENT_METHODS = ["PromptPay", "Card", "Cash", "Transfer", "Other"]

CAT_COLORS = {
    "Food & Drinks":     "#FF6B6B",
    "Transport":         "#4ECDC4",
    "Shopping":          "#45B7D1",
    "Bills & Utilities": "#96CEB4",
    "Healthcare":        "#FFD93D",
    "Entertainment":     "#C77DFF",
    "Education":         "#4CC9F0",
    "Travel":            "#F4A261",
    "Income":            "#06D6A0",
    "Transfer":          "#ADB5BD",
    "Other":             "#868E96",
}

PAYMENT_KEYWORDS = {
    "PromptPay": ["พพ", "พร้อมเพย์", "promptpay", "pp"],
    "Card":      ["บัตร", "card", "visa", "master", "debit", "credit"],
    "Cash":      ["เงินสด", "cash", "สด"],
    "Transfer":  ["โอน", "transfer"],
}

INCOME_KEYWORDS = [
    "เงินเดือน", "salary", "รายได้", "ขาย", "โบนัส", "bonus",
    "income", "ได้รับ", "รับเงิน", "freelance", "ค่าจ้าง",
]

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Food & Drinks": [
        "กาแฟ", "ข้าว", "อาหาร", "กิน", "ดื่ม", "ชา", "น้ำ", "ก๋วยเตี๋ยว",
        "ผัด", "ต้ม", "ปิ้ง", "ทอด", "ส้มตำ", "ขนม", "เค้ก", "ไอศกรีม",
        "ร้านอาหาร", "food", "coffee", "tea", "drink", "meal", "lunch",
        "dinner", "breakfast", "starbucks", "7-11", "เซเว่น",
        "mk", "shabu", "oishi", "mcdonald", "kfc", "burger", "pizza",
        "grab food", "grabfood", "foodpanda", "line man", "robinhood",
        # Thai drink brands
        "เต่าบิน", "ชานม", "ไข่มุก", "bubble tea", "boba", "milk tea",
        "ชาไทย", "ชามะนาว", "โอเลี้ยง", "ลิปตัน", "โอวัลติน",
        "machi", "chabaa", "ochaya", "koi", "tiger sugar",
        "หม่าล่า", "สุกี้", "ราเมน", "ramen", "sushi", "donburi",
    ],
    "Transport": [
        "แท็กซี่", "grab", "น้ำมัน", "bts", "mrt", "รถไฟ", "รถ", "รถเมล์",
        "taxi", "fuel", "bus", "train", "uber", "bolt", "ptt",
        "วินมอเตอร์ไซค์", "มอเตอร์ไซค์", "parking", "ที่จอดรถ",
        "ทางด่วน", "expressway", "เครื่องบิน", "plane", "เรือ",
    ],
    "Shopping": [
        "ห้าง", "ซื้อ", "shop", "lazada", "shopee", "amazon",
        "เสื้อ", "กางเกง", "รองเท้า", "กระเป๋า", "เสื้อผ้า",
        "central", "the mall", "บิ๊กซี", "โลตัส", "makro",
        "ikea", "uniqlo", "zara", "h&m",
    ],
    "Bills & Utilities": [
        "ไฟ", "น้ำ", "internet", "ค่าเช่า", "rent",
        "phone", "มือถือ", "โทรศัพท์", "wifi", "electric", "water",
        "ทรู", "ดีแทค", "ais", "dtac", "true", "pea", "การประปา",
    ],
    "Healthcare": [
        "ยา", "หมอ", "โรงพยาบาล", "คลินิก",
        "hospital", "doctor", "medicine", "pharmacy", "เภสัช",
        "gym", "fitness", "วิตามิน", "vitamin",
    ],
    "Entertainment": [
        "หนัง", "เกม", "netflix", "spotify", "youtube", "disney",
        "คอนเสิร์ต", "movie", "game", "music", "บาร์", "karaoke",
        "bowling", "escape room", "คาเฟ่",
    ],
    "Education": [
        "หนังสือ", "เรียน", "course", "udemy", "coursera",
        "school", "university", "วิชา", "สอน", "ติวเตอร์",
    ],
    "Travel": [
        "โรงแรม", "hotel", "ที่พัก", "เที่ยว", "ท่องเที่ยว",
        "travel", "vacation", "trip", "booking", "airbnb", "agoda",
    ],
}

# ── Secrets helper ────────────────────────────────────────────────────────────

def _secret(key: str) -> str:
    try:
        return st.secrets.get(key, "") or ""
    except Exception:
        return os.environ.get(key, "")


# ── Database (Supabase) ───────────────────────────────────────────────────────

@st.cache_resource
def _get_db():
    if not _SUPABASE_OK:
        return None
    url = _secret("SUPABASE_URL") or st.session_state.get("sb_url", "")
    key = _secret("SUPABASE_KEY") or st.session_state.get("sb_key", "")
    if not url or not key or "your-project" in url:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def db_add(date_val, description, amount, type_, category, payment, notes="", raw="") -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        db.table("transactions").insert({
            "date": str(date_val),
            "description": description,
            "amount": round(float(amount), 2),
            "type": type_,
            "category": category,
            "payment_method": payment,
            "notes": notes,
            "raw_input": raw,
        }).execute()
        return True
    except Exception as e:
        st.error(f"DB insert error: {e}")
        return False


def db_load() -> pd.DataFrame:
    empty = pd.DataFrame(columns=[
        "id","date","description","amount","type",
        "category","payment_method","notes","raw_input","created_at",
    ])
    db = _get_db()
    if not db:
        return empty
    try:
        r = db.table("transactions").select("*").order("date", desc=True).order("id", desc=True).execute()
        if r.data:
            df = pd.DataFrame(r.data)
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
            return df
        return empty
    except Exception as e:
        st.error(f"DB load error: {e}")
        return empty


def db_delete(tx_id: int):
    db = _get_db()
    if db:
        try:
            db.table("transactions").delete().eq("id", tx_id).execute()
        except Exception as e:
            st.error(f"DB delete error: {e}")


def db_category_hint(desc: str) -> str | None:
    """Return the most common past category for a similar description."""
    db = _get_db()
    if not db or not desc:
        return None
    try:
        r = db.table("transactions").select("category").ilike(
            "description", f"%{desc[:12]}%"
        ).execute()
        cats = [row["category"] for row in (r.data or []) if row.get("category")]
        return Counter(cats).most_common(1)[0][0] if cats else None
    except Exception:
        return None


# ── Rule-based parser ─────────────────────────────────────────────────────────

def _detect_payment(text: str) -> str:
    t = text.lower()
    for method, kws in PAYMENT_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return method
    return "Other"


def _detect_category(text: str) -> str:
    t = text.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return cat
    return "Other"


def _is_income(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in INCOME_KEYWORDS)


def rule_parse(text: str) -> dict:
    parts = text.strip().split()
    amount = None
    payment = _detect_payment(text)
    desc_parts: list[str] = []

    all_pay_kws = {kw.lower() for kws in PAYMENT_KEYWORDS.values() for kw in kws}

    for part in parts:
        clean = part.replace(",", "").replace("฿", "")
        if re.match(r"^\d+(\.\d+)?$", clean) and amount is None:
            amount = float(clean)
            continue
        if part.lower() in all_pay_kws:
            continue
        if part.lower() not in {"บาท", "thb", "b.", "฿"}:
            desc_parts.append(part)

    tx_type = "income" if _is_income(text) else "expense"
    return {
        "description":    " ".join(desc_parts) or text,
        "amount":         amount or 0.0,
        "type":           tx_type,
        "category":       "Income" if tx_type == "income" else _detect_category(text),
        "payment_method": payment,
    }


# ── AI parser ─────────────────────────────────────────────────────────────────

@st.cache_resource
def _get_ai():
    if not _ANTHROPIC_OK:
        return None
    key = _secret("ANTHROPIC_API_KEY") or st.session_state.get("ai_key", "")
    if not key or "sk-ant-..." in key:
        return None
    try:
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


_AI_SYSTEM = """You are a Thai/English financial transaction parser.

Return ONLY a valid JSON object with these exact keys:
- description: item/service name (keep original language)
- amount: numeric amount as a number
- type: "expense" or "income"
- category: exactly one of [Food & Drinks, Transport, Shopping, Bills & Utilities, Healthcare, Entertainment, Education, Travel, Income, Transfer, Other]
- payment_method: exactly one of [PromptPay, Card, Cash, Transfer, Other]

Thai payment shortcuts:
- พพ, พร้อมเพย์, pp, qr → PromptPay
- บัตร, card, visa, master, debit, credit → Card
- เงินสด, cash, สด → Cash
- โอน, transfer → Transfer

Income keywords → type="income", category="Income":
เงินเดือน, salary, โบนัส, bonus, รายได้, income, ขาย, freelance, ค่าจ้าง

Use your knowledge of Thai brands:
- LINE MAN, GrabFood, Foodpanda, MK, Shabu, Starbucks, 7-Eleven → Food & Drinks
- เต่าบิน, ชานมไข่มุก, Machi, KOI, Tiger Sugar, Ochaya → Food & Drinks (drink shops)
- BTS, MRT, Grab ride, Bolt, taxi, น้ำมัน, PTT → Transport
- BigC, Lotus, Central, Shopee, Lazada, Uniqlo → Shopping
- AIS, DTAC, True, PEA, ค่าไฟ, ค่าน้ำ, ค่าเช่า → Bills & Utilities
- Netflix, Spotify, Disney+, YouTube Premium → Entertainment

Return ONLY the JSON object, no markdown wrapper."""


def ai_parse(text: str, hint: str | None = None) -> dict:
    client = _get_ai()
    if not client:
        return rule_parse(text)

    system = _AI_SYSTEM
    if hint:
        system += f"\n\nHistory note: similar past descriptions were categorized as '{hint}'."

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)
        result["amount"] = float(result.get("amount", 0))
        if result.get("category") not in CATEGORIES:
            result["category"] = _detect_category(text)
        if result.get("payment_method") not in PAYMENT_METHODS:
            result["payment_method"] = _detect_payment(text)
        return result
    except Exception:
        return rule_parse(text)


def parse_receipt(image_bytes: bytes, media_type: str) -> list[dict]:
    client = _get_ai()
    if not client:
        raise RuntimeError("AI client not available")
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": (
                    "Parse this receipt. Return a JSON array of transactions.\n"
                    'Each item: {"description":"...","amount":0.0,"type":"expense",'
                    '"category":"Food & Drinks","payment_method":"Cash"}\n'
                    "Categories: Food & Drinks, Transport, Shopping, Bills & Utilities, "
                    "Healthcare, Entertainment, Education, Travel, Other\n"
                    "Payment: PromptPay, Card, Cash, Transfer, Other\n"
                    "Use the total paid amount, not individual line items.\n"
                    "Return ONLY a valid JSON array."
                )},
            ],
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```json?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    result = json.loads(raw)
    return result if isinstance(result, list) else [result]


# ── Charts ────────────────────────────────────────────────────────────────────

_T = "plotly_dark"


def _chart_donut(df: pd.DataFrame):
    exp = df[df["type"] == "expense"]
    if exp.empty:
        return None
    by_cat = exp.groupby("category")["amount"].sum().reset_index().sort_values("amount", ascending=False)
    fig = go.Figure(go.Pie(
        labels=by_cat["category"],
        values=by_cat["amount"],
        marker_colors=[CAT_COLORS.get(c, "#868E96") for c in by_cat["category"]],
        hole=0.45,
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b><br>฿%{value:,.0f} (%{percent})<extra></extra>",
    ))
    fig.update_layout(title="Spending by Category", template=_T,
                      height=360, margin=dict(t=50, b=10, l=10, r=10), showlegend=False)
    return fig


def _chart_payment(df: pd.DataFrame):
    exp = df[df["type"] == "expense"]
    if exp.empty:
        return None
    by_pm = exp.groupby("payment_method")["amount"].sum().reset_index().sort_values("amount", ascending=False)
    pm_col = {"PromptPay": "#7B61FF", "Card": "#FF6B6B", "Cash": "#4ECDC4", "Transfer": "#45B7D1", "Other": "#868E96"}
    fig = go.Figure(go.Bar(
        x=by_pm["payment_method"],
        y=by_pm["amount"],
        marker_color=[pm_col.get(p, "#868E96") for p in by_pm["payment_method"]],
        hovertemplate="<b>%{x}</b><br>฿%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(title="By Payment Method", template=_T,
                      height=360, margin=dict(t=50, b=10, l=10, r=10),
                      xaxis_title=None, yaxis_title="฿", showlegend=False)
    return fig


def _chart_daily(df: pd.DataFrame, days: int = 30):
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    d = d[d["date"] >= cutoff]
    fig = go.Figure()
    for type_, color, name in [("expense", "#FF6B6B", "Expense"), ("income", "#06D6A0", "Income")]:
        grp = d[d["type"] == type_].groupby("date")["amount"].sum().reset_index()
        if not grp.empty:
            fig.add_trace(go.Bar(x=grp["date"], y=grp["amount"], name=name, marker_color=color,
                                 hovertemplate="%{x|%b %d}<br>฿%{y:,.0f}<extra>" + name + "</extra>"))
    fig.update_layout(title=f"Daily Cash Flow — Last {days} Days", template=_T, barmode="group",
                      height=300, margin=dict(t=50, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=1.15), xaxis_title=None, yaxis_title="฿")
    return fig


def _chart_monthly(df: pd.DataFrame):
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d["month"] = d["date"].dt.to_period("M").dt.to_timestamp()
    monthly = d.groupby(["month", "type"])["amount"].sum().reset_index()
    fig = go.Figure()
    for type_, color, name in [("expense", "#FF6B6B", "Expense"), ("income", "#06D6A0", "Income")]:
        grp = monthly[monthly["type"] == type_]
        if not grp.empty:
            fig.add_trace(go.Scatter(
                x=grp["month"], y=grp["amount"], mode="lines+markers", name=name,
                line=dict(color=color, width=2.5), marker=dict(size=7),
                hovertemplate="%{x|%b %Y}<br>฿%{y:,.0f}<extra>" + name + "</extra>",
            ))
    fig.update_layout(title="Monthly Trend", template=_T, height=300,
                      margin=dict(t=50, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=1.15), xaxis_title=None, yaxis_title="฿")
    return fig


def _chart_area(df: pd.DataFrame):
    exp = df[df["type"] == "expense"].copy()
    if exp.empty or exp["date"].nunique() < 3:
        return None
    exp["date"] = pd.to_datetime(exp["date"])
    exp["month"] = exp["date"].dt.to_period("M").dt.to_timestamp()
    pivot = exp.groupby(["month", "category"])["amount"].sum().unstack(fill_value=0).reset_index()
    fig = go.Figure()
    for cat in [c for c in CATEGORIES if c in pivot.columns and c not in ("Income", "Transfer")]:
        fig.add_trace(go.Scatter(
            x=pivot["month"], y=pivot[cat], name=cat, stackgroup="one",
            fillcolor=CAT_COLORS.get(cat, "#868E96"),
            line=dict(color=CAT_COLORS.get(cat, "#868E96"), width=0.5),
            hovertemplate="%{x|%b %Y}<br>฿%{y:,.0f}<extra>" + cat + "</extra>",
        ))
    fig.update_layout(title="Category Breakdown Over Time", template=_T, height=320,
                      margin=dict(t=50, b=40, l=10, r=10),
                      legend=dict(orientation="h", y=-0.2, font=dict(size=10)),
                      xaxis_title=None, yaxis_title="฿")
    return fig


# ── UI helpers ────────────────────────────────────────────────────────────────

def _fmt(amount: float, type_: str) -> str:
    return f"{'+'if type_=='income' else '−'}฿{amount:,.0f}"


def _metric_row(df: pd.DataFrame):
    today = date.today()
    this_m = df[
        (pd.to_datetime(df["date"]).dt.month == today.month) &
        (pd.to_datetime(df["date"]).dt.year  == today.year)
    ]
    t_inc = df[df["type"] == "income"]["amount"].sum()
    t_exp = df[df["type"] == "expense"]["amount"].sum()
    net   = t_inc - t_exp
    m_exp = this_m[this_m["type"] == "expense"]["amount"].sum()
    m_inc = this_m[this_m["type"] == "income"]["amount"].sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Income",  f"฿{t_inc:,.0f}")
    c2.metric("Total Expense", f"฿{t_exp:,.0f}")
    c3.metric("Net Balance",   f"฿{net:,.0f}",
              delta=f"{'+'if net>=0 else ''}{net:,.0f}",
              delta_color="normal" if net >= 0 else "inverse")
    c4.metric("This Month",    f"฿{m_exp:,.0f}",
              delta=f"income +฿{m_inc:,.0f}" if m_inc > 0 else None)
    c5.metric("Transactions",  str(len(df)))


# ── Tab: Add ──────────────────────────────────────────────────────────────────

def _tab_add():
    col_l, col_r = st.columns([3, 2], gap="large")

    with col_l:
        st.markdown("### Quick Input")
        st.caption("Type in Thai or English — payment method auto-detected")

        quick = st.text_input(
            "tx",
            placeholder="กาแฟ 50 พพ  |  grab 120 บัตร  |  เงินเดือน 35000 โอน",
            label_visibility="collapsed",
        )

        bc1, bc2 = st.columns([2, 1])
        with bc1:
            parse_btn = st.button("Parse & Preview", type="primary", use_container_width=True)
        with bc2:
            if st.button("Examples", use_container_width=True):
                st.session_state["show_ex"] = not st.session_state.get("show_ex", False)

        if st.session_state.get("show_ex"):
            examples = [
                "กาแฟ 50 พพ", "grab 120 บัตร", "ข้าวกะเพรา 80 เงินสด",
                "เงินเดือน 35000 โอน", "netflix 299 บัตร", "ค่าไฟ 800 โอน",
            ]
            st.caption("Click any example:")
            cols = st.columns(3)
            for i, ex in enumerate(examples):
                if cols[i % 3].button(ex, key=f"ex_{i}"):
                    st.session_state["_quick_ex"] = ex
                    st.rerun()

        if st.session_state.get("_quick_ex"):
            quick = st.session_state.pop("_quick_ex")

        if parse_btn and quick.strip():
            with st.spinner("Parsing..."):
                hint = db_category_hint(quick)
                parsed = ai_parse(quick, hint)
            st.session_state["preview"] = parsed
            st.session_state["preview_raw"] = quick

        if "preview" in st.session_state:
            p = st.session_state["preview"]
            st.markdown("---")
            st.markdown("**Preview — adjust if needed, then save:**")

            r1c1, r1c2 = st.columns(2)
            with r1c1:
                p_desc = st.text_input("Description", value=p.get("description", ""), key="p_desc")
                p_amt  = st.number_input("Amount (฿)", value=float(p.get("amount", 0)),
                                         min_value=0.0, step=1.0, key="p_amt")
                p_type = st.selectbox("Type", ["expense", "income"],
                                      index=0 if p.get("type") != "income" else 1, key="p_type")
            with r1c2:
                cat_i  = CATEGORIES.index(p["category"]) if p.get("category") in CATEGORIES else 0
                p_cat  = st.selectbox("Category", CATEGORIES, index=cat_i, key="p_cat")
                pm_i   = PAYMENT_METHODS.index(p["payment_method"]) if p.get("payment_method") in PAYMENT_METHODS else 4
                p_pay  = st.selectbox("Payment Method", PAYMENT_METHODS, index=pm_i, key="p_pay")
                p_date = st.date_input("Date", value=date.today(), key="p_date")

            sc1, sc2 = st.columns([3, 1])
            with sc1:
                if st.button("Save Transaction", type="primary", use_container_width=True):
                    if p_desc and p_amt > 0:
                        ok = db_add(str(p_date), p_desc, p_amt, p_type, p_cat, p_pay,
                                    raw=st.session_state.get("preview_raw", ""))
                        if ok:
                            st.session_state.pop("preview", None)
                            st.session_state.pop("preview_raw", None)
                            st.success(f"Saved: {p_desc} {_fmt(p_amt, p_type)} [{p_cat} · {p_pay}]")
                            st.rerun()
                    else:
                        st.error("Description and amount are required.")
            with sc2:
                if st.button("Clear", use_container_width=True):
                    st.session_state.pop("preview", None)
                    st.rerun()

    with col_r:
        st.markdown("### Receipt Upload")

        if not _get_ai():
            st.info("Set ANTHROPIC_API_KEY to enable AI receipt parsing.")

        uploaded = st.file_uploader(
            "Receipt image", type=["jpg", "jpeg", "png", "webp"],
            label_visibility="collapsed",
        )

        if uploaded:
            st.image(uploaded, use_column_width=True)
            if _get_ai():
                if st.button("Parse Receipt", type="primary", use_container_width=True):
                    with st.spinner("Reading receipt with AI..."):
                        try:
                            img_bytes = uploaded.read()
                            ext = uploaded.name.rsplit(".", 1)[-1].lower()
                            mt = "image/jpeg" if ext in ("jpg","jpeg") else f"image/{ext}"
                            items = parse_receipt(img_bytes, mt)
                            st.session_state["receipt_items"] = items
                        except Exception as e:
                            st.error(f"Receipt parse failed: {e}")

        if "receipt_items" in st.session_state:
            items = st.session_state["receipt_items"]
            st.markdown(f"**Found {len(items)} item(s):**")
            for i, item in enumerate(items):
                with st.expander(
                    f"{item.get('description','Item')} — ฿{float(item.get('amount',0)):,.0f}",
                    expanded=True,
                ):
                    items[i]["category"] = st.selectbox(
                        "Category", CATEGORIES,
                        index=CATEGORIES.index(item["category"]) if item.get("category") in CATEGORIES else 10,
                        key=f"ri_cat_{i}",
                    )
                    items[i]["payment_method"] = st.selectbox(
                        "Payment", PAYMENT_METHODS,
                        index=PAYMENT_METHODS.index(item["payment_method"]) if item.get("payment_method") in PAYMENT_METHODS else 2,
                        key=f"ri_pay_{i}",
                    )
            if st.button("Save All Items", type="primary", use_container_width=True):
                saved = sum(
                    db_add(str(date.today()),
                           item.get("description","Receipt item"),
                           float(item.get("amount", 0)),
                           item.get("type","expense"),
                           item.get("category","Other"),
                           item.get("payment_method","Cash"),
                           raw="[receipt]")
                    for item in items
                )
                if saved:
                    st.session_state.pop("receipt_items", None)
                    st.success(f"Saved {saved} transactions!")
                    st.rerun()

        st.markdown("---")
        st.markdown("### Manual Entry")
        with st.form("manual"):
            m_desc = st.text_input("Description")
            m_amt  = st.number_input("Amount (฿)", min_value=0.0, step=1.0)
            mc1, mc2 = st.columns(2)
            with mc1:
                m_type = st.selectbox("Type", ["expense", "income"])
                m_cat  = st.selectbox("Category", CATEGORIES)
            with mc2:
                m_pay  = st.selectbox("Payment", PAYMENT_METHODS)
                m_date = st.date_input("Date", value=date.today())
            if st.form_submit_button("Add Transaction", use_container_width=True):
                if m_desc and m_amt > 0:
                    if db_add(str(m_date), m_desc, m_amt, m_type, m_cat, m_pay):
                        st.success(f"Saved: {m_desc}")
                        st.rerun()
                else:
                    st.error("Fill in description and amount.")


# ── Tab: Dashboard ────────────────────────────────────────────────────────────

def _tab_dashboard(df: pd.DataFrame):
    if df.empty:
        st.info("No transactions yet. Add some to see your dashboard.")
        return

    dc1, dc2, _ = st.columns([2, 2, 4])
    with dc1:
        d_from = st.date_input("From", value=date.today().replace(month=1, day=1), key="d_from")
    with dc2:
        d_to = st.date_input("To", value=date.today(), key="d_to")

    mask = (
        (pd.to_datetime(df["date"]) >= pd.Timestamp(d_from)) &
        (pd.to_datetime(df["date"]) <= pd.Timestamp(d_to))
    )
    dff = df[mask]
    if dff.empty:
        st.info("No data in selected date range.")
        return

    _metric_row(dff)
    st.markdown("---")

    r1c1, r1c2 = st.columns(2)
    with r1c1:
        fig = _chart_donut(dff)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
    with r1c2:
        fig = _chart_payment(dff)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

    r2c1, r2c2 = st.columns(2)
    with r2c1:
        days = st.selectbox("Period", [7, 14, 30, 90, 180], index=2, key="cf_days")
        st.plotly_chart(_chart_daily(df, days), use_container_width=True)
    with r2c2:
        fig = _chart_monthly(df)
        if fig:
            st.plotly_chart(fig, use_container_width=True)

    fig = _chart_area(dff)
    if fig:
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    bot1, bot2 = st.columns(2)
    with bot1:
        st.markdown("**Top 10 Expenses**")
        top = dff[dff["type"] == "expense"].nlargest(10, "amount")[
            ["date","description","amount","category","payment_method"]
        ].copy()
        top["amount"] = top["amount"].apply(lambda x: f"฿{x:,.0f}")
        st.dataframe(top, hide_index=True, use_container_width=True)
    with bot2:
        st.markdown("**Category Summary**")
        cs = dff[dff["type"] == "expense"].groupby("category").agg(
            Total=("amount","sum"), Count=("amount","count"), Avg=("amount","mean")
        ).sort_values("Total", ascending=False).reset_index()
        cs["Total"] = cs["Total"].apply(lambda x: f"฿{x:,.0f}")
        cs["Avg"]   = cs["Avg"].apply(lambda x: f"฿{x:,.0f}")
        st.dataframe(cs, hide_index=True, use_container_width=True)


# ── Tab: Transactions ─────────────────────────────────────────────────────────

def _tab_transactions(df: pd.DataFrame):
    if df.empty:
        st.info("No transactions yet.")
        return

    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    with fc1: f_from = st.date_input("From", value=date.today().replace(day=1), key="tx_from")
    with fc2: f_to   = st.date_input("To",   value=date.today(),                key="tx_to")
    with fc3: f_cat  = st.selectbox("Category", ["All"] + CATEGORIES,           key="tx_cat")
    with fc4: f_type = st.selectbox("Type",     ["All","expense","income"],      key="tx_type")
    with fc5: f_pay  = st.selectbox("Payment",  ["All"] + PAYMENT_METHODS,      key="tx_pay")

    fdf = df[
        (pd.to_datetime(df["date"]) >= pd.Timestamp(f_from)) &
        (pd.to_datetime(df["date"]) <= pd.Timestamp(f_to))
    ].copy()
    if f_cat  != "All": fdf = fdf[fdf["category"]       == f_cat]
    if f_type != "All": fdf = fdf[fdf["type"]            == f_type]
    if f_pay  != "All": fdf = fdf[fdf["payment_method"] == f_pay]

    t_exp = fdf[fdf["type"] == "expense"]["amount"].sum()
    t_inc = fdf[fdf["type"] == "income"]["amount"].sum()
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Expense", f"฿{t_exp:,.0f}")
    mc2.metric("Income",  f"฿{t_inc:,.0f}")
    mc3.metric("Net",     f"฿{t_inc - t_exp:,.0f}")
    mc4.metric("Count",   str(len(fdf)))

    st.markdown("---")

    if fdf.empty:
        st.info("No transactions match the selected filters.")
        return

    disp = fdf[["id","date","description","amount","type","category","payment_method"]].copy()
    disp["amount"] = disp.apply(lambda r: _fmt(r["amount"], r["type"]), axis=1)
    disp.columns   = ["ID","Date","Description","Amount","Type","Category","Payment"]
    st.dataframe(disp, hide_index=True, use_container_width=True, height=420)

    ac1, ac2 = st.columns(2)
    with ac1:
        csv = fdf.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Export CSV (Excel-safe)",
            data=csv,
            file_name=f"finance_{f_from}_{f_to}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with ac2:
        del_id = st.number_input("Delete transaction by ID", min_value=0, step=1, key="del_id")
        if st.button("Delete", use_container_width=True) and del_id > 0:
            db_delete(int(del_id))
            st.success(f"Deleted #{del_id}")
            st.rerun()


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _sidebar():
    with st.sidebar:
        st.title("💰 Finance")
        st.markdown("---")

        db = _get_db()
        if db:
            st.success("Database: Connected")
        else:
            st.error("Database: Not connected")
            st.caption("Enter your Supabase credentials:")
            sb_url = st.text_input("Project URL", placeholder="https://xxx.supabase.co", key="sb_url_in")
            sb_key = st.text_input("Anon Key",    type="password",                       key="sb_key_in")
            if sb_url and sb_key and st.button("Connect", use_container_width=True):
                st.session_state["sb_url"] = sb_url
                st.session_state["sb_key"] = sb_key
                _get_db.clear()
                st.rerun()

        ai = _get_ai()
        if ai:
            st.success("AI Parsing: Active")
        else:
            st.warning("AI Parsing: Off")
            api_key = st.text_input("Anthropic API Key", type="password",
                                    placeholder="sk-ant-...", key="ai_key_in")
            if api_key and st.button("Enable AI", use_container_width=True):
                st.session_state["ai_key"] = api_key
                _get_ai.clear()
                st.rerun()

        st.markdown("---")
        st.markdown("**Thai shortcuts**")
        st.markdown("""
| Keyword | Method |
|---|---|
| พพ, pp | PromptPay |
| บัตร | Card |
| เงินสด, สด | Cash |
| โอน | Transfer |
        """)
        st.markdown("---")
        st.caption("Summer Finance Tracker")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Sarabun', sans-serif !important; }
    .block-container { padding-top: 1.5rem; }
    </style>
    """, unsafe_allow_html=True)

    _sidebar()
    st.title("💰 Finance Tracker")

    db = _get_db()
    if not db:
        st.warning("Connect to Supabase in the sidebar to get started.")
        st.markdown("""
**One-time setup (5 minutes):**

**1. Create a free Supabase project**
- Go to [supabase.com](https://supabase.com) → New project (free tier)
- Wait ~1 min for it to provision

**2. Create the transactions table**
- In your project: **SQL Editor** → New query → paste and run:

```sql
CREATE TABLE IF NOT EXISTS transactions (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE NOT NULL,
    description    TEXT NOT NULL,
    amount         NUMERIC(12,2) NOT NULL,
    type           TEXT NOT NULL CHECK (type IN ('expense','income')),
    category       TEXT,
    payment_method TEXT,
    notes          TEXT DEFAULT '',
    raw_input      TEXT DEFAULT '',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Allow the anon key to read/write (required if RLS is enabled)
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon_all" ON transactions FOR ALL TO anon USING (true) WITH CHECK (true);
```

**3. Get your credentials**
- Project Settings → API → copy **Project URL** and **anon public key**

**4. Fill in the sidebar** (or add to `.streamlit/secrets.toml` for local dev)

**5. Deploy to Streamlit Cloud** *(for iPhone access)*
- Push this `finance/` folder to a GitHub repo
- Go to [share.streamlit.io](https://share.streamlit.io) → deploy
- Add your secrets in the Streamlit Cloud dashboard
        """)
        return

    df = db_load()

    tab1, tab2, tab3 = st.tabs(["➕  Add", "📊  Dashboard", "📋  Transactions"])
    with tab1:
        _tab_add()
    with tab2:
        _tab_dashboard(df)
    with tab3:
        _tab_transactions(df)


if __name__ == "__main__":
    main()
