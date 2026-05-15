import http.server, json, os, socketserver, urllib.parse, urllib.request, hmac, hashlib, base64, time, csv, math
from datetime import date, datetime

PORT = 8080

# Portfolio holdings for history calculation
HOLDINGS = {
    'SGOV':  4.64238,  'AMZN':  2.90293, 'AVGO':  1.61999, 'AMD':   1.70,
    'GOOGL': 0.92532, 'MSFT':  0.95708, 'SOFI':  17.4573, 'UBER':  3.9030,
    'NFLX':  2.68,    'ZETA':  11.7032, 'NBIS':  1.14279, 'RBRK':  2.52346,
    'RKLB':  1.85669, 'GLD':   0.085,
    'IVV':   1.9397,  'SCHG':  30.34,   'SMH':   0.577,   'DTCR':  8.713,
    'VXUS':  7.38,
}
PORTFOLIO_START = '2025-09-10'
PORTFOLIO_CASH  = 294  # USD cash buffer

def fetch_history_data():
    try:
        import yfinance as yf
        import pandas as pd

        tickers = list(HOLDINGS.keys()) + ['SPY', 'GC=F']
        raw = yf.download(tickers, start=PORTFOLIO_START, auto_adjust=True, progress=False)
        closes = raw['Close'] if hasattr(raw.columns, 'levels') else raw

        closes = closes.ffill().dropna(how='all')

        port_vals = pd.Series(0.0, index=closes.index)
        for ticker, shares in HOLDINGS.items():
            col = ticker
            if col not in closes.columns and ticker == 'GC=F':
                col = 'GC=F'
            if col in closes.columns:
                port_vals += closes[col].fillna(0) * shares
        port_vals += PORTFOLIO_CASH

        # Gold oz (separate from GLD ETF holding)
        if 'GC=F' in closes.columns:
            port_vals += closes['GC=F'].fillna(0) * 0.107

        # Normalize to % return from first valid day
        first = port_vals.iloc[0]
        spy_first = closes['SPY'].iloc[0] if 'SPY' in closes.columns else 1
        port_ret = ((port_vals / first) - 1) * 100
        spy_ret  = ((closes['SPY'] / spy_first) - 1) * 100 if 'SPY' in closes.columns else pd.Series(0, index=closes.index)

        dates = [str(d.date()) for d in closes.index]
        return {
            'dates':     dates,
            'portfolio': [round(v, 3) for v in port_ret.tolist()],
            'spy':       [round(v, 3) for v in spy_ret.tolist()],
        }
    except Exception as e:
        print(f'[history] error: {e}')
        return {'error': str(e)}

def fetch_prices(symbols_str):
    try:
        import yfinance as yf
        tickers = [s.strip() for s in symbols_str.split(',') if s.strip()]
        result = []
        for sym in tickers:
            try:
                t = yf.Ticker(sym)
                info = t.fast_info
                price = info.last_price
                prev_close = info.previous_close
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                result.append({
                    'symbol': sym,
                    'regularMarketPrice': round(price, 4) if price else None,
                    'regularMarketChange': round(price - prev_close, 4) if price and prev_close else None,
                    'regularMarketChangePercent': round(change_pct, 4) if price else None,
                })
            except Exception as e:
                print(f'[{sym}] error: {e}')
                result.append({'symbol': sym, 'regularMarketPrice': None, 'regularMarketChangePercent': None})

        # Wrap in Yahoo Finance compatible shape so dashboard JS needs no changes
        payload = {'quoteResponse': {'result': result, 'error': None}}
        return json.dumps(payload).encode()
    except ImportError:
        return None

def okx_request(path, api_key, secret, passphrase):
    ts = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
    msg = ts + 'GET' + path
    sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    url = 'https://www.okx.com' + path
    req = urllib.request.Request(url, headers={
        'OK-ACCESS-KEY': api_key,
        'OK-ACCESS-SIGN': sign,
        'OK-ACCESS-TIMESTAMP': ts,
        'OK-ACCESS-PASSPHRASE': passphrase,
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f'[okx] HTTP {e.code} on {path}: {body}')
        raise

EARN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'earn_config.json')

def load_earn_fixed() -> float:
    if os.path.exists(EARN_CONFIG_PATH):
        with open(EARN_CONFIG_PATH) as f:
            return float(json.load(f).get('earn_fixed', 753.0))
    return 753.0

def save_earn_fixed(value: float):
    with open(EARN_CONFIG_PATH, 'w') as f:
        json.dump({'earn_fixed': value}, f)

def fetch_account(api_key, secret, passphrase):
    # Asset valuation — USD totals across Funding + Trading + Earn
    val = okx_request('/api/v5/asset/asset-valuation', api_key, secret, passphrase)
    details    = val['data'][0].get('details', {}) if val.get('data') else {}
    total      = float(val['data'][0].get('totalBal', 0)) if val.get('data') else 0
    trading_eq = float(details.get('trading', 0))
    # Earn API unreliable — persisted in earn_config.json; editable from dashboard
    earn_eq = load_earn_fixed()

    # BTC qty from trading account
    bal = okx_request('/api/v5/account/balance', api_key, secret, passphrase)
    t_details = bal['data'][0]['details'] if bal.get('data') else []
    btc_qty  = next((float(d['cashBal']) for d in t_details if d['ccy'] == 'BTC'), 0)
    usdt_qty = next((float(d['cashBal']) for d in t_details if d['ccy'] == 'USDT'), 0)

    return {'total': total, 'trading_eq': trading_eq, 'earn_eq': earn_eq,
            'btc_qty': btc_qty, 'usdt_qty': usdt_qty}

def fetch_okx_balances():
    keys_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'okx_keys.json')
    with open(keys_path) as f:
        cfg = json.load(f)

    result = {}

    # Main account
    m = cfg['main']
    main = fetch_account(m['api_key'], m['secret'], m['passphrase'])
    result['main_btc']    = main['btc_qty']
    result['main_usdt']   = main['usdt_qty']
    result['trading_eq']  = main['trading_eq']
    result['earn_eq']     = main['earn_eq']
    result['main_total']  = main['total']

    # Sub-accounts — query via main account key using sub-account balance endpoint
    for label in ['KhunAutumn', 'KhunSummer']:
        try:
            path = f'/api/v5/account/subaccount/balances?subAcct={label}'
            data = okx_request(path, m['api_key'], m['secret'], m['passphrase'])
            details = data['data'][0]['details'] if data.get('data') else []
            total_usd = sum(float(d.get('eqUsd', 0)) for d in details)
            result[label] = total_usd
        except Exception as e:
            print(f'[okx] {label} error: {e}')
            result[label] = None

    return result

GMHD_SHEET_ID = '1UdgJOBDPsclCbDO3KozpVsC3phfJLLef00Rp-J0R06M'
GMHD_SHEET_NAME = 'Dashboard'
GMHD_CELL = 'B8'

def fetch_gmhd_price():
    url = (f'https://docs.google.com/spreadsheets/d/{GMHD_SHEET_ID}'
           f'/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(GMHD_SHEET_NAME)}&range={GMHD_CELL}')
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = r.read().decode('utf-8').strip().strip('"')
            price = float(raw.replace(',', ''))
            return price
    except Exception as e:
        print(f'[gmhd] error: {e}')
        return None

def read_txns(path):
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as f:  # utf-8-sig strips BOM
        for r in csv.DictReader(f):
            if r.get('date'):  # skip blank/trailing rows
                rows.append(r)
    return rows

def fetch_pnl_data():
    try:
        import yfinance as yf

        txn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.csv')
        rows = read_txns(txn_path)

        # ── Replay transactions: avg-cost method ──────────────────────────
        holdings = {}   # ticker -> {shares, avg_cost}
        realized  = 0.0
        fees      = 0.0
        interest  = 0.0
        dividends = 0.0

        # Per-account realized P&L
        acct_realized = {}

        for r in sorted(rows, key=lambda x: x['date']):
            action  = r['action']
            ticker  = r['ticker']
            shares  = float(r['shares']) if r['shares'] else 0.0
            price   = float(r['price'])  if r['price']  else 0.0
            comm    = float(r['commission']) if r['commission'] else 0.0
            account = r.get('account', 'Stock')
            fx      = 33.5 if r.get('currency') == 'THB' else 1.0

            fees += comm

            if action == 'Buy':
                h = holdings.setdefault(ticker, {'shares': 0.0, 'avg_cost': 0.0, 'account': account})
                total_cost = h['shares'] * h['avg_cost'] + shares * price + comm
                h['shares'] += shares
                h['avg_cost'] = total_cost / h['shares'] if h['shares'] > 0 else 0.0
                h['account'] = account

            elif action == 'Sell':
                h = holdings.get(ticker)
                if h and h['shares'] > 0:
                    sold = min(shares, h['shares'])
                    gain = (price - h['avg_cost']) * sold - comm
                    realized += gain
                    acct_realized[account] = acct_realized.get(account, 0.0) + gain
                    h['shares'] -= sold
                    if h['shares'] <= 0:
                        del holdings[ticker]

            elif action == 'Interest':
                interest += shares  # 'shares' field holds cash amount for interest

            elif action == 'Dividend':
                dividends += shares * price / fx

        # ── Fetch current prices for unrealized ───────────────────────────
        live_tickers = [t for t in holdings if t not in ('CASH',) and holdings[t]['shares'] > 0.001]
        # Map to yfinance symbols
        yf_overrides = {'SCB-R.BK': 'SCB-R.BK', 'KBANK-R.BK': 'KBANK-R.BK', 'TISCO-R.BK': 'TISCO-R.BK'}
        prices_now = {}
        if live_tickers:
            try:
                raw = yf.download(live_tickers, period='2d', auto_adjust=True, progress=False)
                closes = raw['Close'] if hasattr(raw.columns, 'levels') else raw
                latest = closes.ffill().iloc[-1]
                for t in live_tickers:
                    if t in latest.index and not __import__('math').isnan(float(latest[t])):
                        prices_now[t] = float(latest[t])
            except Exception as e:
                print(f'[pnl] price fetch error: {e}')

        # ── Unrealized per account ─────────────────────────────────────────
        unrealized = 0.0
        acct_unrealized = {}
        acct_value      = {}
        acct_cost       = {}
        per_ticker = {}

        for ticker, h in holdings.items():
            if ticker == 'CASH' or h['shares'] <= 0.001:
                continue
            price_now = prices_now.get(ticker)
            if price_now is None:
                continue
            fx = 33.5 if ticker.endswith('.BK') else 1.0
            cost_total  = h['shares'] * h['avg_cost'] / fx   # avg_cost stored in local currency
            value_total = h['shares'] * price_now / fx
            gain = value_total - cost_total
            unrealized += gain
            acct = h['account']
            acct_unrealized[acct] = acct_unrealized.get(acct, 0.0) + gain
            acct_value[acct]      = acct_value.get(acct, 0.0)      + value_total
            acct_cost[acct]       = acct_cost.get(acct, 0.0)       + cost_total
            per_ticker[ticker] = {
                'shares':    round(h['shares'], 6),
                'avg_cost':  round(h['avg_cost'], 4),
                'price_now': round(price_now, 4),
                'value':     round(value_total, 2),
                'gain':      round(gain, 2),
                'pct':       round(gain / cost_total * 100, 2) if cost_total > 0 else 0,
                'account':   acct,
            }

        total_pnl = unrealized + realized + interest + dividends - fees

        return {
            'unrealized':     round(unrealized, 2),
            'realized':       round(realized, 2),
            'dividends':      round(dividends, 2),
            'fees':           round(-fees, 2),
            'interest':       round(interest, 2),
            'total_pnl':      round(total_pnl, 2),
            'by_account': {
                acct: {
                    'unrealized': round(acct_unrealized.get(acct, 0), 2),
                    'realized':   round(acct_realized.get(acct, 0), 2),
                    'value':      round(acct_value.get(acct, 0), 2),
                    'cost':       round(acct_cost.get(acct, 0), 2),
                    'total_pnl':  round(acct_unrealized.get(acct, 0) + acct_realized.get(acct, 0), 2),
                }
                for acct in set(list(acct_unrealized.keys()) + list(acct_realized.keys()))
            },
            'per_ticker': per_ticker,
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {'error': str(e)}

def fetch_dividends_data():
    try:
        txn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.csv')
        rows = read_txns(txn_path)

        FX = 33.5
        today = date.today()
        ttm_start = date(today.year - 1, today.month, today.day)

        events = []
        by_ticker = {}
        by_month  = {}

        for r in rows:
            if r['action'] != 'Dividend':
                continue
            d = datetime.strptime(r['date'], '%Y-%m-%d').date()
            amount = float(r['shares']) if r['shares'] else 0.0
            fx = FX if r.get('currency') == 'THB' else 1.0
            usd = amount / fx
            ticker  = r['ticker']
            account = r.get('account', '')
            events.append({'date': str(d), 'ticker': ticker, 'amount': round(usd, 4), 'account': account})

            if d >= ttm_start:
                by_ticker[ticker] = by_ticker.get(ticker, 0.0) + usd
                ym = d.strftime('%Y-%m')
                by_month[ym] = by_month.get(ym, 0.0) + usd

        total_ttm = sum(by_ticker.values())

        # Fill missing months in TTM range with 0 — generate 12 months back from today
        from datetime import timedelta
        months_filled = {}
        for i in range(13):  # 12 full months + current partial month
            month_offset = today.month - 12 + i
            y = today.year + (month_offset - 1) // 12
            m = ((month_offset - 1) % 12) + 1
            ym = f'{y:04d}-{m:02d}'
            months_filled[ym] = round(by_month.get(ym, 0.0), 4)

        # 3-month rolling avg for better run-rate estimate (last 3 complete months)
        sorted_months = sorted(months_filled.keys())
        # Exclude current partial month from avg
        complete_months = sorted_months[:-1]
        last3 = [months_filled[m] for m in complete_months[-3:] if months_filled[m] > 0]
        if last3:
            monthly_avg = sum(last3) / len(last3)
        else:
            monthly_avg = total_ttm / max(len([v for v in months_filled.values() if v > 0]), 1)
        daily_avg = monthly_avg / 30.0

        # ── Project upcoming dividends ────────────────────────────────────────
        # For each ticker: find last payment date + estimate frequency, project next
        from datetime import timedelta
        last_div = {}  # ticker -> list of dates
        last_amt = {}  # ticker -> last amount

        for r in rows:
            if r['action'] != 'Dividend':
                continue
            ticker = r['ticker']
            d = datetime.strptime(r['date'], '%Y-%m-%d').date()
            fx = FX if r.get('currency') == 'THB' else 1.0
            usd = float(r['shares']) / fx
            last_div.setdefault(ticker, []).append(d)
            last_amt[ticker] = usd  # last occurrence wins since rows are sorted by date

        upcoming = []
        for ticker, dates in last_div.items():
            dates_sorted = sorted(dates)
            if len(dates_sorted) < 2:
                # Only 1 payment: assume annual (365d) to avoid false monthly projections
                freq_days = 365
            else:
                # Average interval between payments
                intervals = [(dates_sorted[i+1] - dates_sorted[i]).days
                             for i in range(len(dates_sorted)-1)]
                freq_days = sum(intervals) / len(intervals)

            last_date = dates_sorted[-1]
            # Project next payment
            proj_date = last_date + timedelta(days=round(freq_days))
            # Show projections up to 12 months ahead
            if today <= proj_date <= today + timedelta(days=365):
                upcoming.append({
                    'date':    str(proj_date),
                    'ticker':  ticker,
                    'amount':  round(last_amt[ticker], 4),
                    'account': next((e['account'] for e in events if e['ticker'] == ticker), ''),
                    'projected': True,
                })

        upcoming.sort(key=lambda e: e['date'])

        return {
            'events':      sorted(events, key=lambda e: e['date'], reverse=True),
            'upcoming':    upcoming,
            'by_ticker':   {k: round(v, 4) for k, v in sorted(by_ticker.items(), key=lambda x: -x[1])},
            'by_month':    months_filled,
            'total_ttm':   round(total_ttm, 2),
            'monthly_avg': round(monthly_avg, 2),
            'daily_avg':   round(daily_avg, 4),
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {'error': str(e)}

def xirr(cashflows):
    """Compute XIRR given list of (date, amount) tuples. Returns annual rate or None."""
    if len(cashflows) < 2:
        return None
    dates, amounts = zip(*cashflows)
    t0 = dates[0]
    days = [(d - t0).days / 365.0 for d in dates]

    def npv(rate):
        return sum(a / (1 + rate) ** t for a, t in zip(amounts, days))

    # Bisection — expand upper bound until sign flips (handles small first-deposit cases)
    lo, hi = -0.999, 5.0
    try:
        while npv(lo) * npv(hi) > 0 and hi < 1e6:
            hi *= 10
        if npv(lo) * npv(hi) > 0:
            return None
        for _ in range(200):
            mid = (lo + hi) / 2
            if npv(mid) == 0 or (hi - lo) / 2 < 1e-8:
                return mid
            if npv(mid) * npv(lo) < 0:
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2
    except Exception:
        return None

def fetch_returns_data(current_portfolio_usd):
    try:
        import yfinance as yf
        import pandas as pd

        txn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.csv')
        rows = read_txns(txn_path)

        txn_sorted = sorted(rows, key=lambda r: r['date'])

        # ── Deposits ─────────────────────────────────────────────────────────
        deposits = []
        for r in rows:
            if r['action'] == 'Deposit':
                amt = float(r['shares']) if r['shares'] else 0
                fx  = 33.5 if r.get('currency') == 'THB' else 1.0
                deposits.append((datetime.strptime(r['date'], '%Y-%m-%d').date(), amt / fx))

        total_deposited = sum(a for _, a in deposits)

        # MWR is computed after portfolio replay (needs v_start from port_after)
        mwr = None

        # ── Build price history ──────────────────────────────────────────────
        # Dynamically include ALL tickers ever held to avoid survivorship bias.
        # Thai .BK stocks are excluded (unreliable yfinance data for Bangkok exchange).
        _all_txn_tickers = set(
            r['ticker'] for r in rows
            if r['ticker'] and r['ticker'] != 'CASH' and not r['ticker'].endswith('.BK')
        )
        yf_map = {t: t for t in _all_txn_tickers}
        all_yf = list(_all_txn_tickers) + ['SPY']
        import warnings, logging
        logging.getLogger('yfinance').setLevel(logging.ERROR)
        # Start from first transaction date to capture full Sharpe history
        first_txn_date = txn_sorted[0]['date'] if txn_sorted else '2023-01-01'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            hist = yf.download(all_yf, start=first_txn_date, auto_adjust=True, progress=False)
        closes = hist['Close'].ffill() if hasattr(hist.columns, 'levels') else hist.ffill()

        trading_dates = list(closes.index)

        def port_value_at(h, prices_row):
            val = 0.0
            for t, s in h.items():
                col = yf_map.get(t)
                if col and col in prices_row.index:
                    v = prices_row[col]
                    if not pd.isna(v):
                        val += s * float(v)
            return val

        # Build two snapshots per day: BEFORE and AFTER same-day transactions
        # This is the key fix: TWR sub-period uses value_before(CF) / value_after(prev_CF)
        txn_by_date = {}
        for r in txn_sorted:
            txn_by_date.setdefault(r['date'], []).append(r)

        port_before = {}  # value using prev-day's holdings
        port_after  = {}  # value after today's buys/sells
        prev_h = {}

        for dt in trading_dates:
            dt_str = dt.strftime('%Y-%m-%d')
            prow = closes.loc[dt]
            port_before[dt_str] = port_value_at(prev_h, prow)

            cur_h = dict(prev_h)
            for r in txn_by_date.get(dt_str, []):
                t = r['ticker']
                if r['action'] == 'Buy' and r['shares']:
                    cur_h[t] = cur_h.get(t, 0) + float(r['shares'])
                elif r['action'] == 'Sell' and r['shares']:
                    cur_h[t] = max(0, cur_h.get(t, 0) - float(r['shares']))
            port_after[dt_str] = port_value_at(cur_h, prow)
            prev_h = cur_h

        dates_list = sorted(port_after.keys())
        port_vals  = [port_after[d] for d in dates_list]
        spy_closes = {d.strftime('%Y-%m-%d'): float(closes.loc[d, 'SPY'])
                      for d in trading_dates if 'SPY' in closes.columns and not pd.isna(closes.loc[d, 'SPY'])}

        # ── TWR: daily chain-linking, subtract same-day deposits from V_end ───
        # deposit_amounts_by_date: cash actually added to account each day
        deposit_by_date = {}
        for r in rows:
            if r['action'] == 'Deposit':
                amt = float(r['shares']) if r['shares'] else 0
                fx  = 33.5 if r.get('currency') == 'THB' else 1.0
                dep_usd = amt / fx
                deposit_by_date[r['date']] = deposit_by_date.get(r['date'], 0) + dep_usd

        def nearest_trading(d_str):
            for d in dates_list:
                if d >= d_str:
                    return d
            return dates_list[-1]

        # ── MWR — XIRR (true money-weighted IRR) ────────────────────────────────
        # Cash flows: each deposit is an outflow (-), final portfolio value is inflow (+).
        # Solves sum(CF_i / (1+r)^(t_i/365)) = 0 via bisection.
        def xirr(cash_flows):
            if not cash_flows or len(cash_flows) < 2:
                return None
            dates_cf = [d for d, _ in cash_flows]
            d0 = min(dates_cf)
            times = [(d - d0).days / 365.0 for d in dates_cf]
            amounts = [a for _, a in cash_flows]
            def npv(r):
                return sum(a / (1 + r) ** t for a, t in zip(amounts, times))
            # Check sign change exists — required for bisection
            try:
                npv_lo, npv_hi = npv(-0.999), npv(50.0)
                if npv_lo * npv_hi > 0:
                    return None
                lo, hi = -0.999, 50.0
                for _ in range(200):
                    mid = (lo + hi) / 2
                    if npv(mid) > 0:
                        lo = mid
                    else:
                        hi = mid
                    if abs(hi - lo) < 1e-9:
                        break
                return (lo + hi) / 2
            except Exception:
                return None

        mwr = None
        if deposits and current_portfolio_usd > 0:
            # Build XIRR cash flow series: outflows = deposits (negative), inflow = current value today
            cf = [(dep_date, -dep_amt) for dep_date, dep_amt in deposits]
            cf.append((date.today(), current_portfolio_usd))
            mwr = xirr(cf)
            if mwr is not None:
                print(f'[mwr-debug] XIRR={mwr*100:.2f}% over {(date.today()-min(d for d,_ in deposits)).days}d, deposits={len(deposits)}, current=${current_portfolio_usd:.0f}')

        twr_factor = 1.0
        prev_v = None
        # Only start TWR once the portfolio has meaningful size (avoids amplifying tiny early positions)
        MIN_VAL = 200.0

        for dt_str in dates_list:
            v_after = port_after.get(dt_str, 0)

            if prev_v is not None and prev_v >= MIN_VAL:
                v_before = port_before.get(dt_str, v_after)
                if v_before >= MIN_VAL * 0.5:
                    twr_factor *= v_before / prev_v

            if v_after >= MIN_VAL:
                prev_v = v_after
            else:
                prev_v = None

        twr = twr_factor - 1.0

        # SPY return over same period
        spy_vals = [spy_closes.get(d) for d in dates_list]
        spy_start = next((v for v in spy_vals if v), None)
        spy_end   = next((v for v in reversed(spy_vals) if v), None)
        spy_twr   = (spy_end / spy_start - 1.0) if spy_start and spy_end else None

        # ── Sharpe Ratio (sub-period returns — same as TWR, excludes DCA jumps) ─
        # port_before[today] / port_after[yesterday] gives the true market return
        # for the day, stripping out the effect of new deposits/purchases.
        daily_rets = []
        for i in range(1, len(dates_list)):
            prev_d = dates_list[i - 1]
            curr_d = dates_list[i]
            v_prev = port_after.get(prev_d, 0)
            v_curr = port_before.get(curr_d, 0)
            if v_prev > 500 and v_curr > 0:
                daily_rets.append(v_curr / v_prev - 1)

        rf_daily = 0.043 / 252  # ~4.3% SGOV yield (post Fed cuts 2025-2026)
        excess   = [r - rf_daily for r in daily_rets]
        if len(excess) > 20:
            mean_e = sum(excess) / len(excess)
            std_e  = math.sqrt(sum((x - mean_e)**2 for x in excess) / (len(excess)-1))
            sharpe = (mean_e / std_e * math.sqrt(252)) if std_e > 0 else None
        else:
            sharpe = None

        # ── Simulation data for savings vs S&P500 vs portfolio ───────────────
        # For each deposit, compute what SPY would be worth today
        spy_sim = 0.0
        port_sim = current_portfolio_usd  # actual portfolio (already computed)
        spy_prices_by_date = {}
        for dt in trading_dates:
            ds = dt.strftime('%Y-%m-%d')
            if ds in spy_closes:
                spy_prices_by_date[ds] = spy_closes[ds]

        for r in rows:
            if r['action'] == 'Deposit':
                dep_amt = float(r['shares']) / (33.5 if r.get('currency') == 'THB' else 1.0)
                dep_date = nearest_trading(r['date'])
                spy_buy = spy_prices_by_date.get(dep_date)
                spy_now  = spy_vals[-1] if spy_vals else None
                if spy_buy and spy_now:
                    shares_spy = dep_amt / spy_buy
                    spy_sim += shares_spy * spy_now

        # ── A1: total Buy capital & SPY equivalent ───────────────────────────
        invest_amount_usd = 0.0
        invest_spy_sim    = 0.0
        spy_now_val = spy_vals[-1] if spy_vals else None

        for r in txn_sorted:
            if r['action'] != 'Buy':
                continue
            shares_r = float(r['shares'])     if r['shares']     else 0.0
            price_r  = float(r['price'])       if r['price']       else 0.0
            comm_r   = float(r['commission'])  if r['commission']  else 0.0
            fx       = 33.5 if r.get('currency') == 'THB' else 1.0
            amt_usd  = (shares_r * price_r + comm_r) / fx
            invest_amount_usd += amt_usd

            buy_date = nearest_trading(r['date'])
            spy_buy  = spy_closes.get(buy_date)
            if spy_buy and spy_now_val and spy_buy > 0:
                invest_spy_sim += (amt_usd / spy_buy) * spy_now_val

        # Yesterday's portfolio change (last two trading days in port_after)
        yesterday_change = None
        yesterday_pct    = None
        if len(dates_list) >= 2:
            v_today_model = port_after.get(dates_list[-1], 0)
            v_prev_model  = port_after.get(dates_list[-2], 0)
            if v_prev_model > 5:
                yesterday_change = round(v_today_model - v_prev_model, 2)
                yesterday_pct    = round((v_today_model - v_prev_model) / v_prev_model * 100, 2)

        # ── Daily TWR index (chain-linked, starts at 1.0) ─────────────────────
        n_total = len(dates_list)
        twr_idx = [1.0]
        for i in range(1, n_total):
            v_p = port_after.get(dates_list[i-1], 0)
            v_c = port_before.get(dates_list[i], 0)
            factor = (v_c / v_p) if v_p > 50 and v_c > 0 else 1.0
            twr_idx.append(twr_idx[-1] * factor)

        # ── Daily SR index (simple cash-on-cash: port_value / cum_deposits) ──
        sr_idx = []
        cum_dep_running = 0.0
        for d in dates_list:
            cum_dep_running += deposit_by_date.get(d, 0)
            v = port_after.get(d, 0)
            sr_idx.append(round(v / cum_dep_running, 4) if cum_dep_running > 0 else 1.0)

        # ── Daily MWR index (linearly map TWR shape so start=1.0, end=mwr_factor) ─
        mwr_factor = (1 + mwr) if mwr is not None else None
        twr_end    = twr_idx[-1] if twr_idx else 1.0
        twr_range  = twr_end - 1.0
        if mwr_factor and abs(twr_range) > 0.001:
            mwr_range = mwr_factor - 1.0
            mwr_idx = [round(1.0 + (f - 1.0) * mwr_range / twr_range, 4) for f in twr_idx]
        else:
            mwr_idx = [round(f, 4) for f in twr_idx]

        # ── Daily SPY index (normalized to 1.0 at portfolio start) ───────────
        spy_first_v = next((spy_closes[d] for d in dates_list if spy_closes.get(d)), None)
        spy_idx = [round(spy_closes.get(d, spy_first_v or 1) / (spy_first_v or 1), 4)
                   for d in dates_list]

        # Thin series to max 300 points for chart performance
        def thin(lst, target=300):
            if len(lst) <= target:
                return lst
            step = len(lst) / target
            return [lst[int(i * step)] for i in range(target)] + [lst[-1]]

        thin_dates = thin(dates_list)
        thin_idx   = list(range(0, n_total, max(1, n_total // 300)))
        if n_total - 1 not in thin_idx:
            thin_idx.append(n_total - 1)

        # ── Multi-period return table ─────────────────────────────────────────
        ytd_start   = f'{date.today().year}-01-01'
        ytd_si      = next((i for i, d in enumerate(dates_list) if d >= ytd_start), None)

        period_defs = [
            ('1M',  max(0, n_total - 22)),
            ('3M',  max(0, n_total - 64)),
            ('6M',  max(0, n_total - 127)),
            ('YTD', ytd_si),
            ('All', 0),
        ]

        def annualize(r, n_days):
            y = n_days / 252
            return ((1 + r) ** (1 / y) - 1) if y > 0.08 and r > -1 else None

        def pct(v):
            return round(v * 100, 2) if v is not None else None

        periods_out = {}
        for pname, si in period_defs:
            if si is None or si >= n_total - 1:
                periods_out[pname] = None
                continue
            ei     = n_total - 1
            n_days = ei - si

            # TWR
            t = twr_idx[ei] / twr_idx[si] - 1.0
            t_ann = annualize(t, n_days)

            # SR: (V_end - V_start - deposits_added) / V_start
            v_start = port_after.get(dates_list[si], 0)
            dep_added = sum(deposit_by_date.get(d, 0) for d in dates_list[si:])
            s = (current_portfolio_usd - v_start - dep_added) / v_start if v_start > 0 else None
            s_ann = annualize(s, n_days) if s is not None else None

            # MWR: Modified Dietz for the sub-period (v_start = port value at period start)
            m_ann = None
            m     = None
            if v_start > 0:
                t0_p = datetime.strptime(dates_list[si], '%Y-%m-%d').date()
                T_p  = max(n_days, 1)
                late = [(datetime.strptime(d, '%Y-%m-%d').date(), deposit_by_date[d])
                        for d in dates_list[si:] if deposit_by_date.get(d, 0) > 0]
                late_after = [(d, a) for d, a in late if d > t0_p]
                w_cf = sum(a * max(T_p - (d - t0_p).days, 0) / T_p for d, a in late_after)
                net  = sum(a for _, a in late_after)
                denom = v_start + w_cf
                g_p   = current_portfolio_usd - v_start - net
                if denom > 0:
                    md_p  = g_p / denom
                    base_p = 1 + md_p
                    years = n_days / 252
                    if base_p > 0 and years > 0:
                        m_ann = base_p ** (1 / years) - 1
                        m     = md_p

            # SPY
            spy_s = spy_closes.get(dates_list[si])
            spy_e = spy_closes.get(dates_list[ei])
            sp = (spy_e / spy_s - 1) if spy_s and spy_e else None
            sp_ann = annualize(sp, n_days) if sp is not None else None

            periods_out[pname] = {
                'twr':     pct(t),   'twr_ann': pct(t_ann),
                'mwr':     pct(m),   'mwr_ann': pct(m_ann),
                'sr':      pct(s),   'sr_ann':  pct(s_ann),
                'spy':     pct(sp),  'spy_ann': pct(sp_ann),
            }

        chart_out = {
            'dates': [dates_list[i] for i in thin_idx],
            'twr':   [round(twr_idx[i], 4) for i in thin_idx],
            'mwr':   [round(mwr_idx[i], 4) for i in thin_idx],
            'sr':    [round(sr_idx[i],  4) for i in thin_idx],
            'spy':   [round(spy_idx[i], 4) for i in thin_idx],
        }

        return {
            'mwr':              pct(mwr),
            'twr':              round(twr * 100, 2),
            'spy_twr':          round(spy_twr * 100, 2) if spy_twr is not None else None,
            'sharpe':           round(sharpe, 2) if sharpe is not None else None,
            'total_deposited':  round(total_deposited, 2),
            'current_value':    round(current_portfolio_usd, 2),
            'gain_loss':        round(current_portfolio_usd - total_deposited, 2),
            'spy_sim_value':    round(spy_sim, 2),
            'spy_sim_gain':     round(spy_sim - total_deposited, 2),
            'invest_amount':    round(invest_amount_usd, 2),
            'invest_spy_value': round(invest_spy_sim, 2),
            'yesterday_change': yesterday_change,
            'yesterday_pct':    yesterday_pct,
            'periods':          periods_out,
            'chart':            chart_out,
        }
    except Exception as e:
        import traceback
        print(f'[returns] error: {e}')
        traceback.print_exc()
        return {'error': str(e)}

def fetch_simulate_data():
    """Month-by-month: cash savings vs S&P500 vs my portfolio (actual)."""
    try:
        import yfinance as yf
        import pandas as pd

        txn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.csv')
        rows = read_txns(txn_path)

        deposits = [(r['date'], float(r['shares']) / (33.5 if r.get('currency') == 'THB' else 1.0))
                    for r in rows if r['action'] == 'Deposit']
        if not deposits:
            return {'error': 'No deposits found'}

        start_date = deposits[0][0]
        spy = yf.download(['SPY'], start=start_date, auto_adjust=True, progress=False)
        spy_closes = spy['Close'].ffill() if hasattr(spy.columns, 'levels') else spy.ffill()
        spy_closes = spy_closes['SPY'] if 'SPY' in spy_closes.columns else spy_closes

        # Get month-end dates for the chart
        idx = spy_closes.index
        month_ends = {}
        for dt in idx:
            ym = dt.strftime('%Y-%m')
            month_ends[ym] = dt  # last trading day each month

        months_sorted = sorted(month_ends.keys())

        # Compute SPY sim: for each deposit, track share count and value each month
        spy_shares = 0.0
        cash_saved = 0.0
        deposit_idx = 0
        deposits_sorted = sorted(deposits, key=lambda x: x[0])

        # Build cumulative values at each month end
        cash_series = {}
        spy_series  = {}

        dep_ptr = 0
        for ym in months_sorted:
            month_dt = month_ends[ym]
            month_str = month_dt.strftime('%Y-%m-%d')
            # Apply deposits that fall in or before this month
            while dep_ptr < len(deposits_sorted) and deposits_sorted[dep_ptr][0] <= month_str:
                dep_date, dep_amt = deposits_sorted[dep_ptr]
                cash_saved += dep_amt
                # Buy SPY at nearest price on deposit date
                dep_dt_str = dep_date
                # Find closest SPY price on or after deposit date
                spy_at_dep = None
                for dt in idx:
                    if dt.strftime('%Y-%m-%d') >= dep_dt_str:
                        spy_at_dep = float(spy_closes.loc[dt])
                        break
                if spy_at_dep and spy_at_dep > 0:
                    spy_shares += dep_amt / spy_at_dep
                dep_ptr += 1

            spy_price_now = float(spy_closes.loc[month_dt]) if month_dt in spy_closes.index else None
            cash_series[ym] = round(cash_saved, 2)
            spy_series[ym]  = round(spy_shares * spy_price_now, 2) if spy_price_now else None

        return {
            'months':      months_sorted,
            'cash':        [cash_series.get(m) for m in months_sorted],
            'spy':         [spy_series.get(m)  for m in months_sorted],
            'total_deposited': round(cash_saved, 2),
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {'error': str(e)}

TICKER_META = {
    'AMZN':       {'sector':'Consumer Discretionary','industry':'Internet Retail',   'country':'US', 'type':'Stock'},
    'AVGO':       {'sector':'Technology',            'industry':'Semiconductors',     'country':'US', 'type':'Stock'},
    'AMD':        {'sector':'Technology',            'industry':'Semiconductors',     'country':'US', 'type':'Stock'},
    'GOOGL':      {'sector':'Communication Services','industry':'Internet Content',   'country':'US', 'type':'Stock'},
    'MSFT':       {'sector':'Technology',            'industry':'Software',           'country':'US', 'type':'Stock'},
    'SOFI':       {'sector':'Financials',            'industry':'Fintech',            'country':'US', 'type':'Stock'},
    'UBER':       {'sector':'Consumer Discretionary','industry':'Ride-Sharing',       'country':'US', 'type':'Stock'},
    'NFLX':       {'sector':'Communication Services','industry':'Entertainment',      'country':'US', 'type':'Stock'},
    'ZETA':       {'sector':'Technology',            'industry':'Software',           'country':'US', 'type':'Stock'},
    'NBIS':       {'sector':'Technology',            'industry':'AI / Data Centers',  'country':'US', 'type':'Stock'},
    'RBRK':       {'sector':'Technology',            'industry':'Cybersecurity',      'country':'US', 'type':'Stock'},
    'RKLB':       {'sector':'Industrials',           'industry':'Aerospace',          'country':'US', 'type':'Stock'},
    'SGOV':       {'sector':'Fixed Income',          'industry':'Govt Bonds',         'country':'US', 'type':'ETF'},
    'GLD':        {'sector':'Commodities',           'industry':'Gold',               'country':'US', 'type':'ETF'},
    'IVV':        {'sector':'Equity',                'industry':'Broad Market',       'country':'US', 'type':'ETF'},
    'SCHG':       {'sector':'Equity',                'industry':'Growth',             'country':'US', 'type':'ETF'},
    'SMH':        {'sector':'Equity',                'industry':'Semiconductors',     'country':'US', 'type':'ETF'},
    'DTCR':       {'sector':'Equity',                'industry':'Data Centers',       'country':'US', 'type':'ETF'},
    'VXUS':       {'sector':'Equity',                'industry':'International',      'country':'Intl','type':'ETF'},
    'VYMI':       {'sector':'Equity',                'industry':'Intl Dividend',      'country':'Intl','type':'ETF'},
    'JEPQ':       {'sector':'Equity',                'industry':'Covered Call',       'country':'US', 'type':'ETF'},
    'QQQI':       {'sector':'Equity',                'industry':'Covered Call',       'country':'US', 'type':'ETF'},
    'VOO':        {'sector':'Equity',                'industry':'Broad Market',       'country':'US', 'type':'ETF'},
    'SPYI':       {'sector':'Equity',                'industry':'Covered Call',       'country':'US', 'type':'ETF'},
    'SCHD':       {'sector':'Equity',                'industry':'Dividend',           'country':'US', 'type':'ETF'},
    'SCB-R.BK':   {'sector':'Financials',            'industry':'Banking',            'country':'TH', 'type':'Stock'},
    'KBANK-R.BK': {'sector':'Financials',            'industry':'Banking',            'country':'TH', 'type':'Stock'},
    'TISCO-R.BK': {'sector':'Financials',            'industry':'Banking',            'country':'TH', 'type':'Stock'},
}

def fetch_holdings_data():
    try:
        import yfinance as yf
        import pandas as pd
        import warnings, logging
        logging.getLogger('yfinance').setLevel(logging.ERROR)

        txn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.csv')
        rows = read_txns(txn_path)

        # Replay to get current holdings + avg cost
        holdings = {}
        for r in sorted(rows, key=lambda x: x['date']):
            t, action = r['ticker'], r['action']
            shares = float(r['shares']) if r['shares'] else 0.0
            price  = float(r['price'])  if r['price']  else 0.0
            comm   = float(r['commission']) if r['commission'] else 0.0
            if action == 'Buy':
                h = holdings.setdefault(t, {'shares': 0.0, 'avg_cost': 0.0})
                total = h['shares'] * h['avg_cost'] + shares * price + comm
                h['shares'] += shares
                h['avg_cost'] = total / h['shares'] if h['shares'] > 0 else 0.0
            elif action == 'Sell':
                h = holdings.get(t)
                if h:
                    h['shares'] = max(0, h['shares'] - shares)

        live = [t for t, h in holdings.items()
                if t != 'CASH' and h['shares'] > 0.001 and not t.endswith('.BK')]
        bk   = [t for t, h in holdings.items()
                if t != 'CASH' and h['shares'] > 0.001 and t.endswith('.BK')]

        result = {}
        today = date.today()

        def period_offset(label):
            if label == '1d':  return 5   # ~1 trading day back (use 5 cal days to be safe)
            if label == '1w':  return 10
            if label == '1m':  return 35
            if label == '3m':  return 100
            if label == '6m':  return 190
            if label == 'ytd': return (today - date(today.year, 1, 1)).days + 10
            if label == '1y':  return 375
            return 375  # all

        max_days = max(period_offset(p) for p in ['1d','1w','1m','3m','6m','ytd','1y'])
        start_str = (today - __import__('datetime').timedelta(days=max_days + 10)).strftime('%Y-%m-%d')

        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            raw = yf.download(live + ['SPY'], start=start_str, auto_adjust=True, progress=False)

        closes = raw['Close'].ffill() if hasattr(raw.columns, 'levels') else raw.ffill()
        dates_idx = [d.strftime('%Y-%m-%d') for d in closes.index]

        # .BK stocks: just use 2d for 1d gain, skip multi-period
        bk_prices = {}
        if bk:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                bk_raw = yf.download(bk, period='5d', auto_adjust=True, progress=False)
            bk_cl = bk_raw['Close'].ffill() if hasattr(bk_raw.columns, 'levels') else bk_raw.ffill()
            for t in bk:
                if t in bk_cl.columns:
                    vals = bk_cl[t].dropna()
                    if len(vals) >= 2:
                        bk_prices[t] = {'now': float(vals.iloc[-1]), 'prev': float(vals.iloc[-2])}
                    elif len(vals) == 1:
                        bk_prices[t] = {'now': float(vals.iloc[-1]), 'prev': float(vals.iloc[-1])}

        ytd_start = date(today.year, 1, 1).strftime('%Y-%m-%d')

        for ticker, h in holdings.items():
            if ticker == 'CASH' or h['shares'] <= 0.001:
                continue
            shares   = h['shares']
            avg_cost = h['avg_cost']
            fx       = 33.5 if ticker.endswith('.BK') else 1.0

            if ticker.endswith('.BK'):
                bp = bk_prices.get(ticker)
                if not bp:
                    continue
                price_now  = bp['now'] / fx
                price_prev = bp['prev'] / fx
                value_now  = shares * price_now
                cost_basis = shares * avg_cost / fx
                periods = {
                    '1d':  {'gain': round(shares * (price_now - price_prev), 2),
                            'pct':  round((price_now / price_prev - 1) * 100, 2) if price_prev else 0,
                            'start_val': round(shares * price_prev, 2)},
                }
                for p in ['1w','1m','3m','6m','ytd','1y','all']:
                    periods[p] = periods['1d']  # .BK: only 1d available
            else:
                col = ticker if ticker in closes.columns else None
                if col is None:
                    continue
                series = closes[col].dropna()
                if series.empty:
                    continue
                price_now = float(series.iloc[-1])
                value_now = shares * price_now
                cost_basis = shares * avg_cost

                def gain_for(cal_days, ref_date_str=None):
                    if ref_date_str:
                        past = [d for d in dates_idx if d >= ref_date_str]
                        ref_d = past[0] if past else dates_idx[0]
                    else:
                        cutoff = (today - __import__('datetime').timedelta(days=cal_days)).strftime('%Y-%m-%d')
                        past = [d for d in dates_idx if d >= cutoff]
                        ref_d = past[0] if past else dates_idx[0]
                    ref_price = float(closes.loc[ref_d, col]) if ref_d in closes.index.strftime('%Y-%m-%d').tolist() else None
                    if ref_price is None or ref_price == 0:
                        return None
                    g = shares * (price_now - ref_price)
                    p = (price_now / ref_price - 1) * 100
                    sv = shares * ref_price
                    return {'gain': round(g, 2), 'pct': round(p, 2), 'start_val': round(sv, 2)}

                periods = {
                    '1d':  gain_for(5),
                    '1w':  gain_for(10),
                    '1m':  gain_for(35),
                    '3m':  gain_for(100),
                    '6m':  gain_for(190),
                    'ytd': gain_for(0, ytd_start),
                    '1y':  gain_for(375),
                    'all': {
                        'gain':      round(value_now - cost_basis, 2),
                        'pct':       round((value_now / cost_basis - 1) * 100, 2) if cost_basis > 0 else 0,
                        'start_val': round(cost_basis, 2),
                    },
                }
                periods = {k: v for k, v in periods.items() if v is not None}

            meta = TICKER_META.get(ticker, {'sector':'Other','industry':'Other','country':'Other','type':'Other'})
            result[ticker] = {
                'shares':     round(shares, 6),
                'avg_cost':   round(avg_cost, 4),
                'price_now':  round(price_now, 4),
                'value':      round(value_now, 2),
                'cost_basis': round(cost_basis, 2),
                'periods':    periods,
                **meta,
            }

        return {'holdings': result}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {'error': str(e)}


DAILY_LOG_FIELDS = ['date', 'day', 'all', 'stocks', 'etfs', 'gold', 'crypto', 'dividend', 'emergency', 'fx']

def daily_log_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_log.csv')

def read_daily_log():
    path = daily_log_path()
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/prices'):
            self.handle_prices()
        elif self.path.startswith('/api/crypto'):
            self.handle_crypto()
        elif self.path.startswith('/api/gmhd'):
            self.handle_gmhd()
        elif self.path.startswith('/api/history'):
            self.handle_history()
        elif self.path.startswith('/api/returns'):
            self.handle_returns()
        elif self.path.startswith('/api/pnl'):
            self.handle_pnl()
        elif self.path.startswith('/api/dividends'):
            self.handle_dividends()
        elif self.path.startswith('/api/holdings'):
            self.handle_holdings()
        elif self.path.startswith('/api/simulate'):
            self.handle_simulate()
        elif self.path.startswith('/api/daily-log'):
            self.handle_daily_log_get()
        else:
            super().do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/log-daily'):
            self.handle_log_daily()
        elif self.path.startswith('/api/earn-fixed'):
            self.handle_earn_fixed_post()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_daily_log_get(self):
        rows = read_daily_log()
        body = json.dumps({'rows': rows}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_log_daily(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length)) if length else {}
            today = payload.get('date', str(date.today()))

            existing = read_daily_log()
            if any(r['date'] == today for r in existing):
                resp = json.dumps({'status': 'exists', 'date': today}).encode()
            else:
                row = {k: payload.get(k, '') for k in DAILY_LOG_FIELDS}
                row['date'] = today
                write_header = not os.path.exists(daily_log_path())
                with open(daily_log_path(), 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=DAILY_LOG_FIELDS)
                    if write_header:
                        writer.writeheader()
                    writer.writerow(row)
                print(f'[daily-log] recorded {today}: total=${row["all"]}')
                resp = json.dumps({'status': 'ok', 'date': today}).encode()

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            print(f'[daily-log] error: {e}')

    def handle_earn_fixed_post(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length)) if length else {}
            value = float(payload['value'])
            save_earn_fixed(value)
            print(f'[earn] updated earn_fixed to {value}')
            resp = json.dumps({'status': 'ok', 'earn_fixed': value}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            print(f'[earn] error: {e}')

    def handle_simulate(self):
        print('[simulate] computing scenarios...')
        data = fetch_simulate_data()
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_holdings(self):
        print('[holdings] computing per-ticker holdings data...')
        data = fetch_holdings_data()
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_dividends(self):
        print('[dividends] computing dividend data...')
        data = fetch_dividends_data()
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_pnl(self):
        print('[pnl] computing P&L breakdown...')
        data = fetch_pnl_data()
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_returns(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        portfolio_val = float(params.get('value', ['0'])[0])
        print(f'[returns] computing MWR/TWR/SR for portfolio=${portfolio_val:.0f}...')
        data = fetch_returns_data(portfolio_val)
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_history(self):
        print('[history] building portfolio history...')
        data = fetch_history_data()
        body = json.dumps(data).encode()
        self.send_response(200 if 'error' not in data else 502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_crypto(self):
        print('[okx] fetching balances...')
        try:
            data = fetch_okx_balances()
            body = json.dumps(data).encode()
            self.send_response(200)
        except Exception as e:
            print(f'[okx] error: {e}')
            body = json.dumps({'error': str(e)}).encode()
            self.send_response(502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_gmhd(self):
        print('[gmhd] fetching from Google Sheets...')
        price = fetch_gmhd_price()
        if price is not None:
            body = json.dumps({'price': price}).encode()
            self.send_response(200)
        else:
            body = json.dumps({'error': 'Could not fetch GMHD price — check sheet is shared (anyone with link)'}).encode()
            self.send_response(502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def handle_prices(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        symbols = params.get('symbols', [''])[0]
        print(f'[prices] fetching: {symbols[:80]}...')

        body = fetch_prices(symbols)
        if body:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        else:
            err = json.dumps({'error': 'yfinance not installed — run: pip install yfinance'}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, fmt, *args):
        pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))
print(f'Dashboard  →  http://localhost:{PORT}/dashboard.html')
print('Press Ctrl+C to stop.\n')
with socketserver.TCPServer(('', PORT), Handler) as httpd:
    httpd.serve_forever()
