#!/usr/bin/env python3
import urllib.request
import json
import math
import os
import re
import subprocess
import time

def _cache_key(network, pool_address, preset, timeframe, side):
    # Solana keeps its historical prefix so existing cache entries stay valid;
    # other networks (e.g. "robinhood") get their own namespace.
    prefix = "sol:dlmm:indicators" if network == "solana" else f"{network}:indicators"
    return f"{prefix}:{pool_address}:{preset}:{timeframe}:{side}"

def get_cached_indicator(pool_address, preset, timeframe, side, network="solana"):
    """
    Checks Redis for a cached indicator calculation result.
    Returns True, False, or None (if no cache exists).
    """
    key = _cache_key(network, pool_address, preset, timeframe, side)
    try:
        res = subprocess.run(f"redis-cli get \"{key}\"", shell=True, capture_output=True, text=True, timeout=5)
        out = res.stdout.strip()
        if not out or out == "(nil)":
            return None
        if out.lower() == "true":
            return True
        if out.lower() == "false":
            return False
    except Exception as e:
        print(f"Warning: Failed to read indicator cache: {e}")
    return None

def set_cached_indicator(pool_address, preset, timeframe, side, confirmed, ttl=270, network="solana"):
    """
    Saves the calculated indicator result to Redis with a TTL.
    """
    key = _cache_key(network, pool_address, preset, timeframe, side)
    confirmed_str = "true" if confirmed else "false"
    try:
        subprocess.run(f"redis-cli setex \"{key}\" {ttl} {confirmed_str}", shell=True, capture_output=True, timeout=5)
    except Exception as e:
        print(f"Warning: Failed to set indicator cache: {e}")

def fetch_ohlcv_candles(pool_address, timeframe, token_address=None, network="solana"):
    """
    Fetches raw OHLCV candles from GeckoTerminal API.
    Supports retries with backoff, and falls back to token address endpoint if pool address fails.
    network: GeckoTerminal network slug ("solana", "robinhood", ...).
    """
    # Map timeframe to minutes / aggregate candle size
    tf_path = "minute"
    aggregate = 15
    
    clean_tf = str(timeframe).lower().strip()
    if clean_tf in ["5m", "30m"]:
        if clean_tf == "5m":
            aggregate = 5
        else:
            aggregate = 15 # aggregate=15 is supported, aggregate=30 falls back to 15-min aggregate or similar
    else:
        aggregate = 15 # default to 15-min aggregate for 1h/2h/4h/12h/24h
        
    urls = []
    # 1. Primary path: pool address
    urls.append((f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/{tf_path}?aggregate={aggregate}", "pool"))
    # 2. Fallback path: token address (if provided; never the quote asset —
    # wrapped SOL on Solana, since its token-level candles are not the pool's)
    if token_address and token_address != "So11111111111111111111111111111111111111112":
        urls.append((f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token_address}/ohlcv/{tf_path}?aggregate={aggregate}", "token"))
        
    for url, path_type in urls:
        retries = 3
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json;version=20230203"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                    ohlcv_data = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                    if ohlcv_data:
                        # Success
                        reversed_list = list(ohlcv_data)
                        reversed_list.reverse()
                        return reversed_list
            except Exception as e:
                # Handle rate limit (429) or other temporary issues
                status_code = getattr(e, "code", None)
                print(f"Warning: GeckoTerminal {path_type} OHLCV fetch failed (attempt {attempt+1}/{retries}): {e}")
                if status_code == 429:
                    print("Rate limit hit (429). Waiting 2 seconds before retry...")
                    time.sleep(2)
                elif status_code == 404 and path_type == "pool":
                    print("Pool address not indexed (404). Falling back to token address endpoint.")
                    break
                else:
                    time.sleep(1)
                    
    return []

def calculate_rsi(closes, period=2):
    """
    Calculates Relative Strength Index (RSI) using Wilder's smoothing.
    """
    if len(closes) < period + 1:
        return [50.0] * len(closes)
        
    rsi_list = [50.0] * len(closes)
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
            
    # Initial average gain and loss
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rs = 100.0
    else:
        rs = avg_gain / avg_loss
    rsi_list[period] = 100.0 - (100.0 / (1.0 + rs))
    
    for i in range(period + 1, len(closes)):
        gain = gains[i-1]
        loss = losses[i-1]
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rs = 100.0
        else:
            rs = avg_gain / avg_loss
            
        rsi_list[i] = 100.0 - (100.0 / (1.0 + rs))
        
    return rsi_list

def calculate_sma(values, period):
    """
    Calculates Simple Moving Average (SMA).
    """
    if len(values) < period:
        return [sum(values)/len(values) if len(values) > 0 else 0.0] * len(values)
        
    sma = [0.0] * len(values)
    for i in range(len(values)):
        if i < period - 1:
            sma[i] = sum(values[:i+1]) / (i+1)
        else:
            sma[i] = sum(values[i - period + 1 : i + 1]) / period
    return sma

def calculate_standard_deviation(values, sma, period):
    """
    Calculates standard deviation of values over period.
    """
    std = [0.0] * len(values)
    for i in range(len(values)):
        if i < period - 1:
            std[i] = 0.0
        else:
            window = values[i - period + 1 : i + 1]
            mean = sma[i]
            variance = sum((x - mean)**2 for x in window) / period
            std[i] = math.sqrt(variance)
    return std

def calculate_bollinger_bands(closes, period=20, num_std=2):
    """
    Calculates upper, middle (SMA), and lower Bollinger Bands.
    """
    sma = calculate_sma(closes, period)
    std = calculate_standard_deviation(closes, sma, period)
    
    lower = [0.0] * len(closes)
    upper = [0.0] * len(closes)
    
    for i in range(len(closes)):
        lower[i] = sma[i] - (num_std * std[i])
        upper[i] = sma[i] + (num_std * std[i])
        
    return lower, sma, upper

def calculate_atr(highs, lows, closes, period=10):
    """
    Calculates Average True Range (ATR).
    """
    if len(closes) < 2:
        return [0.0] * len(closes)
        
    tr = [0.0] * len(closes)
    tr[0] = highs[0] - lows[0]
    
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        c_prev = closes[i-1]
        tr[i] = max(h - l, abs(h - c_prev), abs(l - c_prev))
        
    # ATR is Wilder's MA of True Range
    atr = [0.0] * len(closes)
    atr[0] = tr[0]
    
    if len(closes) < period:
        return calculate_sma(tr, len(closes))
        
    # Initial SMA of True Range
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(closes)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        
    # Fill in index prior to period
    for i in range(period - 1):
        atr[i] = sum(tr[:i+1]) / (i+1)
        
    return atr

def calculate_supertrend(highs, lows, closes, atr_period=10, multiplier=3.0):
    """
    Calculates Supertrend indicator value, direction, and trend changes.
    """
    n = len(closes)
    if n < atr_period:
        return [0.0] * n, ["bullish"] * n, [False] * n, [False] * n
        
    atr = calculate_atr(highs, lows, closes, atr_period)
    
    basic_upper = [0.0] * n
    basic_lower = [0.0] * n
    
    for i in range(n):
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_upper[i] = hl2 + (multiplier * atr[i])
        basic_lower[i] = hl2 - (multiplier * atr[i])
        
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    trend = [1] * n # 1 for bullish, -1 for bearish
    
    final_upper[0] = basic_upper[0]
    final_lower[0] = basic_lower[0]
    
    for i in range(1, n):
        # Final Lower Band
        if basic_lower[i] > final_lower[i-1] or closes[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
            
        # Final Upper Band
        if basic_upper[i] < final_upper[i-1] or closes[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
            
        # Direction
        if closes[i] > final_upper[i-1]:
            trend[i] = 1
        elif closes[i] < final_lower[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
            if trend[i] == 1 and final_lower[i] < final_lower[i-1]:
                final_lower[i] = final_lower[i-1]
            if trend[i] == -1 and final_upper[i] > final_upper[i-1]:
                final_upper[i] = final_upper[i-1]
                
    supertrend_vals = [0.0] * n
    supertrend_dirs = ["bullish"] * n
    break_ups = [False] * n
    break_downs = [False] * n
    
    for i in range(n):
        supertrend_vals[i] = final_lower[i] if trend[i] == 1 else final_upper[i]
        supertrend_dirs[i] = "bullish" if trend[i] == 1 else "bearish"
        if i > 0:
            break_ups[i] = (trend[i] == 1 and trend[i-1] == -1)
            break_downs[i] = (trend[i] == -1 and trend[i-1] == 1)
            
    return supertrend_vals, supertrend_dirs, break_ups, break_downs

def calculate_fibonacci(highs, lows):
    """
    Calculates Fibonacci Retracement Levels.
    """
    if not highs or not lows:
        return {"0.500": 0.0, "0.618": 0.0, "0.786": 0.0}
    max_high = max(highs)
    min_low = min(lows)
    diff = max_high - min_low
    return {
        "0.500": max_high - 0.500 * diff,
        "0.618": max_high - 0.618 * diff,
        "0.786": max_high - 0.786 * diff
    }

def check_local_indicators(pool_address, base_mint, side, preset, timeframe, network="solana"):
    """
    Executes timing checks using indicators calculated locally from GeckoTerminal candles.
    Checks Redis cache first. Falls back gracefully on failure.
    network: GeckoTerminal network slug ("solana", "robinhood", ...).
    """
    label = (base_mint or pool_address)[:8]
    # Exit checks never use cache — trailing TP fires every 20s and needs fresh data.
    # A stale cached REJECTED blocks trailing exits during reversals for up to 4.5min.
    # Entry checks cache for 270s (deploy happens once; spam prevention is worth it).
    if side == "entry":
        cached_val = get_cached_indicator(pool_address, preset, timeframe, side, network=network)
        if cached_val is not None:
            print(f"📊 Indicator timing check for {label} ({preset}) retrieved from Redis cache: {'🟢 CONFIRMED' if cached_val else '🔴 REJECTED'}")
            return cached_val

    print(f"📊 Running local indicators check for pool {pool_address[:8]} ({preset})")

    # 2. Fetch Candles
    candles = fetch_ohlcv_candles(pool_address, timeframe, token_address=base_mint, network=network)
    if not candles:
        print("Warning: GeckoTerminal returned empty candles — data unavailable, indicator skipped (fail-open: None).")
        return None

    if len(candles) < 30:
        print(f"Warning: Not enough candle history ({len(candles)} candles < 30) — data unavailable, indicator skipped (fail-open: None).")
        return None
        
    # Extract values
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    
    # Calculate indicators
    rsi_list = calculate_rsi(closes, period=7)
    bb_lower, bb_middle, bb_upper = calculate_bollinger_bands(closes, period=20, num_std=2)
    st_vals, st_dirs, st_break_ups, st_break_downs = calculate_supertrend(highs, lows, closes, atr_period=10, multiplier=3.0)
    fibonacci = calculate_fibonacci(highs, lows)
    
    # Latest candle index
    i = len(closes) - 1
    
    close = closes[i]
    prev_close = closes[i-1] if i > 0 else close
    rsi = rsi_list[i]
    lower_band = bb_lower[i]
    upper_band = bb_upper[i]
    supertrend_val = st_vals[i]
    supertrend_dir = st_dirs[i]
    supertrend_break_up = st_break_ups[i]
    supertrend_break_down = st_break_downs[i]
    
    fib50 = fibonacci.get("0.500")
    fib618 = fibonacci.get("0.618")
    fib786 = fibonacci.get("0.786")
    
    def crossed_up(level):
        if level is None or close is None or prev_close is None:
            return False
        return prev_close < level and close >= level
        
    def crossed_down(level):
        if level is None or close is None or prev_close is None:
            return False
        return prev_close > level and close <= level

    # Evaluate presets
    oversold = 30
    overbought = 80
    confirmed = False
    
    if preset == "supertrend_break":
        if side == "entry":
            confirmed = supertrend_break_up or (supertrend_dir == "bullish" and close >= supertrend_val)
        else:
            confirmed = supertrend_break_down or (supertrend_dir == "bearish" and close <= supertrend_val)
            
    elif preset == "rsi_reversal":
        if side == "entry":
            confirmed = rsi is not None and rsi <= oversold
        else:
            confirmed = rsi is not None and rsi >= overbought
            
    elif preset == "bollinger_reversion":
        if side == "entry":
            confirmed = close <= lower_band
        else:
            confirmed = close >= upper_band
            
    elif preset == "rsi_plus_supertrend":
        if side == "entry":
            confirmed = (rsi is not None and rsi <= oversold) and (supertrend_break_up or supertrend_dir == "bullish")
        else:
            confirmed = (rsi is not None and rsi >= overbought) and (supertrend_break_down or supertrend_dir == "bearish")
            
    elif preset == "supertrend_or_rsi":
        if side == "entry":
            confirmed = supertrend_break_up or (supertrend_dir == "bullish" and close >= supertrend_val) or (rsi is not None and rsi <= oversold)
        else:
            confirmed = supertrend_break_down or (supertrend_dir == "bearish" and close <= supertrend_val) or (rsi is not None and rsi >= overbought)
            
    elif preset == "bb_plus_rsi":
        if side == "entry":
            confirmed = close <= lower_band and rsi is not None and rsi <= oversold
        else:
            confirmed = close >= upper_band and rsi is not None and rsi >= overbought
            
    elif preset == "fibo_reclaim":
        if side == "entry":
            confirmed = crossed_up(fib618) or crossed_up(fib50) or crossed_up(fib786)
        else:
            confirmed = crossed_up(fib618) or crossed_up(fib50)
            
    elif preset == "fibo_reject":
        if side == "entry":
            confirmed = crossed_down(fib618) or crossed_down(fib50)
        else:
            confirmed = crossed_down(fib618) or crossed_down(fib50) or crossed_down(fib786)
            
    print(f"📊 Local Timing Check for {label} ({preset}): {'🟢 CONFIRMED' if confirmed else '🔴 REJECTED'} (Close: {close:.8f}, RSI: {rsi:.1f}, ST: {supertrend_dir})")

    # 3. Write to Cache (entry only — exit checks must stay uncached)
    if side == "entry":
        set_cached_indicator(pool_address, preset, timeframe, side, confirmed, network=network)
    
    return confirmed

if __name__ == "__main__":
    # Test execution
    test_pool = "AeUfFU6LU159YSBQvhLbXmh5bW2BqCgAFi5zUSQMnUc7" # CHANCE-SOL
    test_mint = "JCKwsT8UAbygnFkZ7u3amDUM7BXRtwUhCsHQv2khpump" # CHANCE
    check_local_indicators(test_pool, test_mint, "entry", "supertrend_or_rsi", "24h")
