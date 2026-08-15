from aiohttp import (
    ClientResponseError,
    ClientSession,
    ClientTimeout
)
from aiohttp_socks import ProxyConnector
from colorama import Fore, Style, init
from datetime import datetime
import asyncio, base64, json, os, pytz, random, shutil, time

init(autoreset=True)

wib = pytz.timezone('Asia/Jakarta')

# ===== OptimAi payload 生成 (移植自 generate_payload.js, 支持只填 refreshToken) =====
def _fib_shift(i):
    # 必须用 float64 复刻 JS 的 number 行为：Fib 超过 2^53 后 JS 会丢失精度，
    # 服务端正是按 JS 这个"带误差"的斐波那契来校验混淆。用 Python 大整数会算出
    # 不同的 t%20，导致 register-v2 返回 400 Invalid message。
    t, a = 0.0, 1.0
    for _ in range(i):
        t, a = a, t + a
    return int(t) % 20

def _bs(s):
    return ''.join(chr(ord(c) + _fib_shift(i)) for i, c in enumerate(s))

def _rs(s):
    return ''.join(chr((ord(c) ^ (i % 256)) & 255) for i, c in enumerate(s))

def _ss(s):
    t = list(s)
    for i in range(0, len(t) - 1, 2):
        t[i], t[i + 1] = t[i + 1], t[i]
    return ''.join(t)

def _ur(s):
    # 对应 JS 的 btoa(): 按 Latin1 逐字符 1 字节编码(非 UTF-8), 否则 128-255 的字符会编成 2 字节导致 base64 错位
    return base64.b64encode(_ss(_rs(_bs(s))).encode('latin-1')).decode('ascii')

def build_register_payload(user_id, timestamp):
    obj = {
        "user_id": user_id,
        "device_info": {
            "cpu_cores": 1, "memory_gb": 0, "screen_width_px": 375,
            "screen_height_px": 600, "color_depth": 30, "scale_factor": 1,
                "browser_name": "chrome", "device_type": "extension",
                "language": "en-US", "timezone": "Asia/Shanghai"
        },
        "browser_name": "chrome", "device_type": "extension", "timestamp": timestamp
    }
    return _ur(json.dumps(obj, separators=(',', ':')))

def build_uptime_payload(user_id, device_id, timestamp):
    obj = {
        "duration": 600000, "user_id": user_id, "device_id": device_id,
        "device_type": "extension", "timestamp": timestamp
    }
    return _ur(json.dumps(obj, separators=(',', ':')))

class Optimai:
    def __init__(self) -> None:
        # 固定真实浏览器 UA，避免 fake_useragent 运行时联网拉取失败导致 bot 起不来
        self.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        self.BASE_API = "https://api.optimai.network"
        self.headers = {
            "Accept": "*/*",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": "chrome-extension://njlfcjdojmopagogfpjgcbnpmiknapnd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Storage-Access": "active",
            "User-Agent": self.USER_AGENT
        }
        self.proxies = []
        self.proxy_index = 0
        self.account_proxies = {}
        self.access_tokens = {}
        self.access_token_exp = {}   # refresh_token -> access token 的 exp 时间戳(秒)，用于提前预刷新
        self.ACCESS_PRE_REFRESH_SEC = 300   # access token 剩不到这么久就提前换，避免请求撞 401
        self.token_name = {}  # refresh_token -> 显示名
        # refresh token 被服务端判定失效（400/401/403）后放进来，永久跳过，避免死循环刷接口
        self.dead_tokens = set()
        # 429 限流退避：endpoint -> 当前等待秒数，成功后清零
        self.backoff = {}
        self.BACKOFF_START = 15
        self.BACKOFF_MAX = 120
        # 仪表盘：每个账号的实时状态（token -> {text, color}），由 render_dashboard 统一渲染
        self.account_status = {}
        self.start_time = 0
        # register 返回的真实 device_id 与 user_id，uptime 心跳必须用真值 device_id 网页才认节点在线
        self.device_ids = {}
        self.user_ids = {}
        # 并发刷新信号量：限制同一时刻打 /auth/refresh 的账号数，避免 60 个账号同时从同一 IP
        # 齐刷刷刷新把出口 IP 打进 429 限流（限流按 IP 算）。8 路并发足够温和。
        self.auth_sem = asyncio.Semaphore(8)
        # refresh token 轮换映射：原始 refresh_token(启动 key) -> 服务端轮换后返回的最新 refresh_token
        # 服务端 /auth/refresh 可能返回新 refresh token，把它记下来，后续所有调用都用最新的；
        # key 始终用启动时的原始 token，这样 account_status / access_tokens 等字典不必改名，循环代码无需改动。
        self._refresh_map = {}

    def clear_terminal(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def log(self, message):
        print(
            f"{Fore.CYAN + Style.BRIGHT}[ {datetime.now().astimezone(wib).strftime('%x %X %Z')} ]{Style.RESET_ALL}"
            f"{Fore.WHITE + Style.BRIGHT} | {Style.RESET_ALL}{message}",
            flush=True
        )

    def welcome(self):
        print(
            f"""
        {Fore.GREEN + Style.BRIGHT}Auto Ping {Fore.BLUE + Style.BRIGHT}Optimai - BOT
            """
            f"""
        {Fore.GREEN + Style.BRIGHT}Rey? {Fore.YELLOW + Style.BRIGHT}<INI WATERMARK>
            """
        )

    def format_seconds(self, seconds):
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    def load_accounts(self):
        filename = "tokens.txt"
        try:
            if not os.path.exists(filename):
                self.log(f"{Fore.RED}File {filename} Not Found.{Style.RESET_ALL}")
                return []

            raw = [l.strip() for l in open(filename, encoding='utf-8')
                   if l.strip() and not l.lstrip().startswith('#')]
            accounts = []
            for idx, line in enumerate(raw, 1):
                # 支持两种格式:
                #   纯 token:        eyJhbGci...
                #   带名字:          gdgzsy1:eyJhbGci...
                name = None
                token = line
                if ':' in line and not line.startswith('eyJ'):
                    head, _, tail = line.partition(':')
                    if tail.strip().startswith('eyJ'):
                        name, token = head.strip(), tail.strip()
                token = token.strip()

                if not token.startswith('eyJ'):
                    self.log(f"{Fore.YELLOW}跳过第 {idx} 行: 不是 JWT token{Style.RESET_ALL}")
                    continue

                user_id = self.decode_user_id(token)
                if not user_id:
                    self.log(f"{Fore.YELLOW}跳过第 {idx} 行: 无法从 token 解码 userId{Style.RESET_ALL}")
                    continue

                device_id = f"{user_id}-device"
                ts = int(time.time() * 1000)
                # 记录 user_id，供 uptime 心跳用真值 device_id 重建 payload
                self.user_ids[token] = user_id
                accounts.append({
                    "name": name or user_id[:8],
                    "refreshToken": token,
                    "registerPayload": build_register_payload(user_id, ts),
                    "uptimePayload": build_uptime_payload(user_id, device_id, ts),
                })

            if not accounts:
                self.log(f"{Fore.RED}No valid token loaded from {filename}.{Style.RESET_ALL}")
            return accounts

        except Exception as e:
            self.log(f"{Fore.RED + Style.BRIGHT}Failed To Load Tokens: {e}{Style.RESET_ALL}")
            return []

    async def load_proxies(self, use_proxy_choice: int):
        filename = "proxy.txt"
        try:
            if use_proxy_choice == 1:
                async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                    async with session.get("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt") as response:
                        response.raise_for_status()
                        content = await response.text()
                        with open(filename, 'w') as f:
                            f.write(content)
                        self.proxies = content.splitlines()
            else:
                if not os.path.exists(filename):
                    self.log(f"{Fore.RED + Style.BRIGHT}File {filename} Not Found.{Style.RESET_ALL}")
                    return
                with open(filename, 'r') as f:
                    self.proxies = f.read().splitlines()

            if not self.proxies:
                self.log(f"{Fore.RED + Style.BRIGHT}No Proxies Found.{Style.RESET_ALL}")
                return

            self.log(
                f"{Fore.GREEN + Style.BRIGHT}Proxies Total  : {Style.RESET_ALL}"
                f"{Fore.WHITE + Style.BRIGHT}{len(self.proxies)}{Style.RESET_ALL}"
            )

        except Exception as e:
            self.log(f"{Fore.RED + Style.BRIGHT}Failed To Load Proxies: {e}{Style.RESET_ALL}")
            self.proxies = []

    def check_proxy_schemes(self, proxies):
        schemes = ["http://", "https://", "socks4://", "socks5://"]
        if any(proxies.startswith(scheme) for scheme in schemes):
            return proxies
        return f"http://{proxies}"

    def get_next_proxy_for_account(self, email):
        if email not in self.account_proxies:
            if not self.proxies:
                return None
            proxy = self.check_proxy_schemes(self.proxies[self.proxy_index])
            self.account_proxies[email] = proxy
            self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
        return self.account_proxies[email]

    def rotate_proxy_for_account(self, email):
        if not self.proxies:
            return None
        proxy = self.check_proxy_schemes(self.proxies[self.proxy_index])
        self.account_proxies[email] = proxy
        self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
        return proxy

    def mask_account(self, account):
        if "@" in account:
            local, domain = account.split('@', 1)
            mask_account = local[:3] + '*' * 3 + local[-3:]
            return f"{mask_account}@{domain}"

        mask_account = account[:3] + '*' * 3 + account[-3:]
        return mask_account

    def decode_user_id(self, token):
        try:
            p = token.split(".")[1]
            p += "=" * (-len(p) % 4)
            d = json.loads(base64.urlsafe_b64decode(p))
            return d.get("sub") or d.get("userId") or d.get("user_id") or d.get("id")
        except Exception:
            return None

    def get_token_remaining_days(self, token):
        try:
            p = token.split(".")[1]
            p += "=" * (-len(p) % 4)
            d = json.loads(base64.urlsafe_b64decode(p))
            exp = d.get("exp")
            if not exp:
                return None
            return (exp - time.time()) / 86400
        except Exception:
            return None

    def days_color_tag(self, days):
        if days is None:
            return Fore.RED, "无效/无法解码"
        if days < 0:
            return Fore.RED, f"已过期 {-days:.0f}d"
        if days < 3:
            return Fore.RED, f"{days:.0f}d"
        if days < 7:
            return Fore.YELLOW, f"{days:.0f}d"
        return Fore.GREEN, f"{days:.0f}d"

    def _grid_days(self, days):
        """网格节点用的紧凑天数标签（纯 ASCII，避免中文宽度导致对齐错位）。"""
        if days is None:
            return "?"
        if days < 0:
            return f"E{-days:.0f}"      # E5 = 已过期 5 天
        return f"{days:.0f}d"

    def render_dashboard(self, accounts, use_proxy):
        """PERCEPTRON 风格仪表盘：顶部统计条 + 网格节点（带两位序号、多列平铺），
        原地刷新（clear 整屏重绘，不滚动日志）。"""
        self.clear_terminal()

        online = sum(1 for a in accounts
                     if self.account_status.get(a["refreshToken"], {}).get("text") == "在线")
        total = len(accounts)
        elapsed = time.time() - self.start_time if self.start_time else 0
        mode = "代理" if use_proxy else "无代理"
        bar = (f" OptimAi BOT  |  模式: {mode}  |  在线 {online}/{total}  "
               f"|  运行 {self.format_seconds(elapsed)} ")

        # 列数按终端宽度自适应，节点以网格平铺（不再竖排单行）
        try:
            term_w = shutil.get_terminal_size((80, 24)).columns
        except Exception:
            term_w = 80
        cell_w = 11
        cols = max(1, term_w // cell_w)
        width = max(54, len(bar), cols * cell_w)

        dead_texts = ("已判废", "令牌错误", "注册失败", "注册异常")

        print(f"{Fore.CYAN + Style.BRIGHT}{'=' * width}{Style.RESET_ALL}")
        print(f"{Fore.CYAN + Style.BRIGHT}{bar}{Style.RESET_ALL}")
        print(f"{Fore.CYAN + Style.BRIGHT}{'=' * width}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}●在线{Style.RESET_ALL}  "
              f"{Fore.YELLOW}○进行中{Style.RESET_ALL}  "
              f"{Fore.RED}✕失效{Style.RESET_ALL}   "
              f"(格式: [序号]状态剩余天)")
        print(f"{Fore.CYAN + Style.BRIGHT}{'-' * width}{Style.RESET_ALL}")

        for i, acc in enumerate(accounts, 1):
            rt = acc["refreshToken"]
            st = self.account_status.get(rt, {"text": "启动中", "color": Fore.YELLOW})
            text = st.get("text", "启动中")
            if text == "在线":
                mark, scolor = "●", Fore.GREEN
            elif text in dead_texts:
                mark, scolor = "✕", Fore.RED
            else:
                mark, scolor = "○", Fore.YELLOW
            days = self.get_token_remaining_days(self.current_refresh(rt))
            dcolor, _ = self.days_color_tag(days)
            day_tag = self._grid_days(days)
            status_vis = f"[{i:02d}]{mark}"
            pad = " " * max(0, cell_w - len(status_vis) - len(day_tag))
            cell = scolor + status_vis + dcolor + day_tag + Style.RESET_ALL + pad
            print(cell, end="")
            if i % cols == 0:
                print()
        print()

        print(f"{Fore.CYAN + Style.BRIGHT}{'-' * width}{Style.RESET_ALL}")

    def decode_response_data(self, data):
        decoded = base64.b64decode(data).decode('utf-8')
        filtered = ''.join([char for i, char in enumerate(decoded) if (i + 1) % 5 != 0])
        reversed_str = filtered[::-1]
        a = 7

        result = ''.join(
            chr(int(reversed_str[i:i+2], 16) ^ (a + i//2))
            for i in range(0, len(reversed_str), 2)
        )
        return json.loads(result)

    def print_message(self, account, proxy, color, message):
        name = self.token_name.get(account, account)
        days = self.get_token_remaining_days(account)
        day_tag = ""
        if days is not None:
            c, t = self.days_color_tag(days)
            day_tag = f" {c + Style.BRIGHT}剩余{t}{Style.RESET_ALL}"
        self.log(
            f"{Fore.CYAN + Style.BRIGHT}[ Account:{Style.RESET_ALL}"
            f"{Fore.WHITE + Style.BRIGHT} {name} {Style.RESET_ALL}"
            f"{Fore.MAGENTA + Style.BRIGHT}-{Style.RESET_ALL}"
            f"{Fore.CYAN + Style.BRIGHT} Proxy: {Style.RESET_ALL}"
            f"{Fore.WHITE + Style.BRIGHT}{proxy}{Style.RESET_ALL}"
            f"{Fore.MAGENTA + Style.BRIGHT} - {Style.RESET_ALL}"
            f"{Fore.CYAN + Style.BRIGHT}Status:{Style.RESET_ALL}"
            f"{color + Style.BRIGHT} {message} {Style.RESET_ALL}"
            f"{day_tag}"
            f"{Fore.CYAN + Style.BRIGHT}]{Style.RESET_ALL}"
        )

    def print_question(self):
        while True:
            try:
                print("1. Run With Monosans Proxy")
                print("2. Run With Private Proxy")
                print("3. Run Without Proxy")
                choose = int(input("Choose [1/2/3] -> ").strip())

                if choose in [1, 2, 3]:
                    proxy_type = (
                        "Run With Monosans Proxy" if choose == 1 else
                        "Run With Private Proxy" if choose == 2 else
                        "Run Without Proxy"
                    )
                    print(f"{Fore.GREEN + Style.BRIGHT}{proxy_type} Selected.{Style.RESET_ALL}")
                    return choose
                else:
                    print(f"{Fore.RED + Style.BRIGHT}Please enter either 1, 2 or 3.{Style.RESET_ALL}")
            except ValueError:
                print(f"{Fore.RED + Style.BRIGHT}Invalid input. Enter a number (1, 2 or 3).{Style.RESET_ALL}")

    async def wait_rate_limit(self, endpoint: str, refresh_token: str = None, proxy=None):
        """遇到 429 时按指数退避等待。同一 endpoint 全局共享退避时长，
        因为限流是按出口 IP 算的，所有账号必须一起降速才有意义。"""
        delay = self.backoff.get(endpoint, 0)
        delay = self.BACKOFF_START if delay == 0 else min(delay * 2, self.BACKOFF_MAX)
        self.backoff[endpoint] = delay
        if refresh_token:
            self.set_status(refresh_token, f"限流退避{delay}s", Fore.YELLOW)
        # 抖动 ±40%，避免所有账号锁步醒来后又齐刷刷撞 429 形成死循环
        await asyncio.sleep(delay * (0.6 + random.random() * 0.8))

    def clear_rate_limit(self, endpoint: str):
        """请求成功，解除该 endpoint 的退避状态。"""
        if self.backoff.get(endpoint):
            self.backoff[endpoint] = 0

    def mark_dead(self, refresh_token: str, proxy=None, reason: str = ""):
        """标记 refresh token 已失效，后续轮次直接跳过该账号。"""
        if refresh_token not in self.dead_tokens:
            self.dead_tokens.add(refresh_token)
            self.set_status(refresh_token, "已判废", Fore.RED)

    def set_status(self, refresh_token: str, text: str, color):
        """更新某账号在仪表盘上的实时状态（只存不打印，由 render_dashboard 统一渲染）。"""
        self.account_status[refresh_token] = {"text": text, "color": color}

    def current_refresh(self, key: str) -> str:
        """解析某账号当前实际使用的 refresh token（经过轮换后可能是新串）。"""
        return self._refresh_map.get(key, key)

    @staticmethod
    def _decode_exp(token: str):
        """解 JWT 的 exp 字段，返回过期时间戳(秒)；解不出返回 None。"""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==").decode())
            return payload.get("exp")
        except Exception:
            return None

    def get_access_token_remaining_sec(self, refresh_token: str):
        """当前 access token 还剩多少秒过期；无记录返回 None。"""
        exp = self.access_token_exp.get(refresh_token)
        if not exp:
            return None
        return exp - time.time()

    def _extract_refresh_token(self, result):
        """从 /auth/refresh 响应里尽量找出轮换回来的新 refresh token（兼容多种字段/嵌套）。"""
        if not isinstance(result, dict):
            return None
        candidates = [result]
        if isinstance(result.get("data"), dict):
            candidates.append(result["data"])
        if isinstance(result.get("tokens"), dict):
            candidates.append(result["tokens"])
        for obj in candidates:
            for k in ("refresh_token", "refreshToken"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("eyJ"):
                    return v
        return None

    def apply_token_rotation(self, original_key: str, old_rt: str, new_rt: str, new_access):
        """把服务端轮换回来的新 refresh token 落地：更新内存映射 + 写回 tokens.txt。
        原始 key 不变，循环里 self.access_tokens[original_key] / account_status[original_key] 继续有效。"""
        # 1) 内存映射：后续所有调用用最新 refresh token
        self._refresh_map[original_key] = new_rt
        # 2) 刷新 access token（key 仍是 original_key，循环代码无需改动）
        if new_access:
            self.access_tokens[original_key] = new_access
        # 3) 持久化回 tokens.txt：原地替换旧 token 串，保留注释与顺序
        try:
            with open("tokens.txt", encoding="utf-8") as f:
                content = f.read()
            if old_rt in content:
                content = content.replace(old_rt, new_rt)
                with open("tokens.txt", "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as e:
            self.log(f"{Fore.YELLOW}写回 tokens.txt 失败(内存已更新): {e}{Style.RESET_ALL}")
        name = self.token_name.get(original_key, new_rt[:8])
        self.log(
            f"{Fore.MAGENTA + Style.BRIGHT}♻️ token 已自动续期并写回 tokens.txt"
            f"（{name}，旧剩余已过期则忽略）{Style.RESET_ALL}"
        )

    async def ensure_fresh_access_token(self, refresh_token: str, use_proxy: bool, threshold=None):
        """access token 快过期时提前换，避免请求撞 401 抖动。无记录/已判废则跳过。"""
        if refresh_token in self.dead_tokens:
            return
        if threshold is None:
            threshold = self.ACCESS_PRE_REFRESH_SEC
        remain = self.get_access_token_remaining_sec(refresh_token)
        if remain is None:
            # 没有过期信息（启动异常/刚恢复），直接取一次确保有 token
            await self.process_get_access_token(refresh_token, use_proxy)
            return
        if remain < threshold:
            self.set_status(refresh_token, f"即将过期({int(remain)}s)，预刷新", Fore.YELLOW)
            await self.process_get_access_token(refresh_token, use_proxy)

    async def get_access_token(self, refresh_token: str, proxy=None, retries=5):
        # 用当前最新（可能已被轮换）的 refresh token 去请求；key 仍是启动时的原始 token
        rt = self.current_refresh(refresh_token)
        url = f"{self.BASE_API}/auth/refresh"
        data = json.dumps({"refresh_token": rt})
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Authorization": f"Bearer {rt}",
            "Content-Length": str(len(data)),
            "Content-Type": "application/json",
            "Origin": "https://node.optimai.network",
            "Referer": "https://node.optimai.network/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": self.USER_AGENT
        }
        for attempt in range(retries):
            connector = ProxyConnector.from_url(proxy) if proxy else None
            try:
                async with self.auth_sem:
                    async with ClientSession(connector=connector, timeout=ClientTimeout(total=120)) as session:
                        async with session.post(url=url, headers=headers, data=data) as response:
                            # 400/401/403 = 服务端明确拒绝这个 refresh token，重试多少次都没用，直接判废
                            if response.status in (400, 401, 403):
                                self.mark_dead(refresh_token, proxy, f"HTTP {response.status}")
                                return None
                            if response.status == 429:
                                await self.wait_rate_limit("auth/refresh", refresh_token, proxy)
                                continue
                            response.raise_for_status()
                            result = await response.json()
                            self.clear_rate_limit("auth/refresh")
                            access_token = result.get("access_token")
                            # 若服务端在响应里轮换了 refresh token，则自动续期并写回 tokens.txt
                            new_rt = self._extract_refresh_token(result)
                            if new_rt and new_rt != rt:
                                self.apply_token_rotation(refresh_token, rt, new_rt, access_token)
                            return access_token
            except (Exception, ClientResponseError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(5)
                    continue
                self.set_status(refresh_token, "令牌错误", Fore.RED)
                return None

    async def register_nodes(self, refresh_token: str, register_payload: str, use_proxy: bool, proxy=None, retries=5):
        url = f"{self.BASE_API}/devices/register-v2"
        data = json.dumps({"data":register_payload})
        headers = {
            **self.headers,
            "Authorization": f"Bearer {self.access_tokens[refresh_token]}",
            "Content-Length": str(len(data)),
            "Content-Type": "application/json"
        }
        for attempt in range(retries):
            connector = ProxyConnector.from_url(proxy) if proxy else None
            try:
                async with ClientSession(connector=connector, timeout=ClientTimeout(total=120)) as session:
                    async with session.post(url=url, headers=headers, data=data) as response:
                        if response.status == 401:
                            await self.process_get_access_token(refresh_token, use_proxy)
                            if refresh_token in self.dead_tokens:
                                return None
                            headers["Authorization"] = f"Bearer {self.access_tokens[refresh_token]}"
                            continue
                        if response.status == 429:
                            await self.wait_rate_limit("devices/register-v2", refresh_token, proxy)
                            continue
                        response.raise_for_status()
                        self.clear_rate_limit("devices/register-v2")
                        return await response.json()
            except (Exception, ClientResponseError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(5)
                    continue
                self.set_status(refresh_token, "注册失败", Fore.RED)
                return None

    async def update_uptime(self, refresh_token: str, uptime_payload: str, use_proxy: bool, proxy=None, retries=5):
        url = f"{self.BASE_API}/uptime/online"
        data = json.dumps({"data":uptime_payload})
        headers = {
            **self.headers,
            "Authorization": f"Bearer {self.access_tokens[refresh_token]}",
            "Content-Length": str(len(data)),
            "Content-Type": "application/json"
        }
        for attempt in range(retries):
            connector = ProxyConnector.from_url(proxy) if proxy else None
            try:
                async with ClientSession(connector=connector, timeout=ClientTimeout(total=120)) as session:
                    async with session.post(url=url, headers=headers, data=data) as response:
                        if response.status == 401:
                            await self.process_get_access_token(refresh_token, use_proxy)
                            if refresh_token in self.dead_tokens:
                                return None
                            headers["Authorization"] = f"Bearer {self.access_tokens[refresh_token]}"
                            continue
                        if response.status == 429:
                            await self.wait_rate_limit("uptime/online", refresh_token, proxy)
                            continue
                        response.raise_for_status()
                        self.clear_rate_limit("uptime/online")
                        return await response.json()
            except (Exception, ClientResponseError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(5)
                    continue
                return None

    async def perform_checkin(self, refresh_token: str, use_proxy: bool, proxy=None, retries=5):
        url = f"{self.BASE_API}/daily-tasks/check-in"
        headers = {
            **self.headers,
            "Authorization": f"Bearer {self.access_tokens[refresh_token]}",
            "Content-Length": "2",
            "Content-Type": "application/json"
        }
        for attempt in range(retries):
            connector = ProxyConnector.from_url(proxy) if proxy else None
            try:
                async with ClientSession(connector=connector, timeout=ClientTimeout(total=120)) as session:
                    async with session.post(url=url, headers=headers, json={}) as response:
                        if response.status == 401:
                            await self.process_get_access_token(refresh_token, use_proxy)
                            if refresh_token in self.dead_tokens:
                                return None
                            headers["Authorization"] = f"Bearer {self.access_tokens[refresh_token]}"
                            continue
                        if response.status == 429:
                            await self.wait_rate_limit("daily-tasks/check-in", refresh_token, proxy)
                            continue
                        response.raise_for_status()
                        self.clear_rate_limit("daily-tasks/check-in")
                        return await response.json()
            except (Exception, ClientResponseError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(5)
                    continue
                return None

    async def process_get_access_token(self, refresh_token: str, use_proxy: bool):
        if refresh_token in self.dead_tokens:
            return None
        proxy = self.get_next_proxy_for_account(refresh_token) if use_proxy else None
        access_token = None
        while access_token is None:
            access_token = await self.get_access_token(refresh_token, proxy)
            # token 已被判废，立即退出循环，不再骚扰接口
            if refresh_token in self.dead_tokens:
                return None
            if not access_token:
                proxy = self.rotate_proxy_for_account(refresh_token) if use_proxy else None
                await asyncio.sleep(5)
                continue

            self.access_tokens[refresh_token] = access_token
            self.access_token_exp[refresh_token] = self._decode_exp(access_token)
            self.set_status(refresh_token, "认证成功", Fore.GREEN)
            return self.access_tokens[refresh_token]

    async def process_register_nodes(self, refresh_token: str, register_payload: str, uptime_payload: str, use_proxy: bool):
        proxy = self.get_next_proxy_for_account(refresh_token) if use_proxy else None
        nodes = None
        while nodes is None:
            if refresh_token in self.dead_tokens:
                return
            nodes = await self.register_nodes(refresh_token, register_payload, use_proxy, proxy)
            if not nodes:
                proxy = self.rotate_proxy_for_account(refresh_token) if use_proxy else None
                # 注册失败时按退避节奏等待，避免 5 秒一轮把出口 IP 打进限流
                await asyncio.sleep(max(5, self.backoff.get("devices/register-v2", 0) or 15))
                continue

            register_response = nodes.get("data", {})
            register_result = self.decode_response_data(register_response)
            if register_result and register_result.get("device_id"):
                # 保存服务端分配的真实 device_id，uptime 心跳必须用它网页才认节点在线
                self.device_ids[refresh_token] = register_result["device_id"]
                self.set_status(refresh_token, "在线", Fore.GREEN)

                await self.process_update_uptime(refresh_token, uptime_payload, use_proxy)

            nodes = None
            self.set_status(refresh_token, "注册异常", Fore.RED)
            await asyncio.sleep(5)
            continue

    async def process_update_uptime(self, refresh_token: str, uptime_payload: str, use_proxy: bool):
        while True:
            proxy = self.get_next_proxy_for_account(refresh_token) if use_proxy else None
            # access token 快过期时提前预刷新（每 10 分钟心跳顺带检查一次），消除 401 抖动
            await self.ensure_fresh_access_token(refresh_token, use_proxy)
            # 用 register 返回的真实 device_id + 当前时间戳重建 uptime payload；
            # 兜底用启动时构造的占位 payload（device_id=user_id-device），保证即使没拿到真值也能上报
            uid = self.user_ids.get(refresh_token)
            dev = self.device_ids.get(refresh_token)
            if uid and dev:
                payload = build_uptime_payload(uid, dev, int(time.time() * 1000))
            else:
                payload = uptime_payload
            await self.update_uptime(refresh_token, payload, use_proxy, proxy)
            await asyncio.sleep(10 * 60)

    async def process_perform_checkin(self, refresh_token: str, use_proxy: bool):
        while True:
            proxy = self.get_next_proxy_for_account(refresh_token) if use_proxy else None
            # 签到前也确保 token 新鲜，避免 12h 周期内 access 过期后第一次签到撞 401
            await self.ensure_fresh_access_token(refresh_token, use_proxy)
            await self.perform_checkin(refresh_token, use_proxy, proxy)
            await asyncio.sleep(12 * 60 * 60)

    async def process_accounts(self, refresh_token: str, register_payload: str, uptime_payload: str, use_proxy: bool):
        # 首发错峰：每个账号随机等 0~60s 再首次刷新，避免 60 个账号启动瞬间齐刷刷打 /auth/refresh
        await asyncio.sleep(random.uniform(0, 60))
        self.access_tokens[refresh_token] = await self.process_get_access_token(refresh_token, use_proxy)
        if self.access_tokens[refresh_token]:
            tasks = []
            tasks.append(asyncio.create_task(self.process_perform_checkin(refresh_token, use_proxy)))
            tasks.append(asyncio.create_task(self.process_register_nodes(refresh_token, register_payload, uptime_payload, use_proxy)))
            await asyncio.gather(*tasks)

    async def main(self):
        try:
            accounts = self.load_accounts()
            if not accounts:
                self.log(f"{Fore.RED + Style.BRIGHT}No Accounts Loaded.{Style.RESET_ALL}")
                return

            use_proxy_choice = self.print_question()

            use_proxy = False
            if use_proxy_choice in [1, 2]:
                use_proxy = True

            for acc in accounts:
                self.token_name[acc["refreshToken"]] = acc["name"]

            self.clear_terminal()
            self.welcome()
            self.log(
                f"{Fore.GREEN + Style.BRIGHT}Account's Total: {Style.RESET_ALL}"
                f"{Fore.WHITE + Style.BRIGHT}{len(accounts)}{Style.RESET_ALL}"
            )

            # 初始化仪表盘状态：每个账号先标记"启动中"
            self.start_time = time.time()
            for acc in accounts:
                self.account_status[acc["refreshToken"]] = {"text": "启动中", "color": Fore.YELLOW}

            if use_proxy:
                await self.load_proxies(use_proxy_choice)

            # 启动所有账号的后台任务（独立长期协程，内部含 uptime/checkin 的无限循环，
            # 不能放进主循环 await，否则主循环会被 gather 永久阻塞、仪表盘冻结不刷新）
            tasks = []
            for account in accounts:
                refresh_token = account["refreshToken"]
                # 已判废的 token 不启动任务，避免每轮去撞 400
                if refresh_token in self.dead_tokens:
                    continue
                if refresh_token and account["registerPayload"] and account["uptimePayload"]:
                    tasks.append(asyncio.create_task(
                        self.process_accounts(refresh_token, account["registerPayload"], account["uptimePayload"], use_proxy)
                    ))

            if not tasks:
                self.log(f"{Fore.RED + Style.BRIGHT}所有 token 均已失效，无可运行账号。请更新 tokens.txt 后重启。{Style.RESET_ALL}")
                return

            # 主循环只负责原地刷新仪表盘；账号任务在后台持续运行并自行更新状态
            while True:
                self.render_dashboard(accounts, use_proxy)
                for t in tasks:
                    if t.done() and not t.cancelled() and t.exception():
                        self.log(f"{Fore.RED + Style.BRIGHT}账号任务异常: {t.exception()}{Style.RESET_ALL}")
                await asyncio.sleep(10)

        except Exception as e:
            self.log(f"{Fore.RED+Style.BRIGHT}Error: {e}{Style.RESET_ALL}")
            raise e

if __name__ == "__main__":
    try:
        bot = Optimai()
        asyncio.run(bot.main())
    except KeyboardInterrupt:
        print(
            f"{Fore.CYAN + Style.BRIGHT}[ {datetime.now().astimezone(wib).strftime('%x %X %Z')} ]{Style.RESET_ALL}"
            f"{Fore.WHITE + Style.BRIGHT} | {Style.RESET_ALL}"
            f"{Fore.RED + Style.BRIGHT}[ EXIT ] Optimai - BOT{Style.RESET_ALL}"
        )
