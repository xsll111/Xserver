name: XServer VPS Auto Renew

on:
  workflow_dispatch:
  schedule:
    # 每 6 小时跑一次（UTC），你在日志里用 Asia/Shanghai 显示时间
    - cron: "0 */6 * * *"

permissions:
  contents: write

concurrency:
  group: xserver-vps-renew
  cancel-in-progress: false

jobs:
  renew:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install system deps (Xvfb + fonts)
        run: |
          sudo apt-get update
          sudo apt-get install -y xvfb \
            libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 \
            fonts-noto-cjk fonts-noto-color-emoji

      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          # 你脚本用到的：playwright、aiohttp、playwright-stealth(可选)
          pip install playwright aiohttp playwright-stealth
          python -m playwright install --with-deps chromium

      - name: Preflight config check
        shell: bash
        run: |
          echo "🚀 开始执行 XServer VPS 自动续期任务..."
          echo "⏰ 执行时间: $(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')"
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo "📋 配置检查:"
          if [ -n "$XSERVER_EMAIL" ]; then echo "  ✅ XSERVER_EMAIL: 已配置"; else echo "  ❌ XSERVER_EMAIL: 未配置"; fi
          if [ -n "$XSERVER_PASSWORD" ]; then echo "  ✅ XSERVER_PASSWORD: 已配置"; else echo "  ❌ XSERVER_PASSWORD: 未配置"; fi
          if [ -n "$XSERVER_VPS_ID" ]; then echo "  ✅ XSERVER_VPS_ID: $XSERVER_VPS_ID"; else echo "  ❌ XSERVER_VPS_ID: 未配置"; fi
          if [ -n "$PROXY_SERVER" ]; then echo "  ✅ PROXY_SERVER: 已配置"; else echo "  ℹ️ PROXY_SERVER: 未配置"; fi
          if [ -n "$CAPTCHA_API_URL" ]; then echo "  ✅ CAPTCHA_API_URL: 已配置"; else echo "  ℹ️ CAPTCHA_API_URL: 未配置(将使用脚本默认值)"; fi
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          test -f renewal.py || (echo "❌ 找不到 renewal.py"; ls -lah; exit 1)
        env:
          XSERVER_EMAIL: ${{ secrets.XSERVER_EMAIL }}
          XSERVER_PASSWORD: ${{ secrets.XSERVER_PASSWORD }}
          XSERVER_VPS_ID: ${{ secrets.XSERVER_VPS_ID }}
          PROXY_SERVER: ${{ secrets.PROXY_SERVER }}
          CAPTCHA_API_URL: ${{ secrets.CAPTCHA_API_URL }}

      - name: Run renewal (with Xvfb)
        shell: bash
        run: |
          set -e
          mkdir -p artifacts
          # 用虚拟显示器运行（支持 headless=False）
          xvfb-run -a -s "-screen 0 1920x1080x24" python3 renewal.py || true

          # 收集产物（不管成功失败都尽量打包）
          cp -f renewal.log artifacts/renewal.log || true
          cp -f README.md artifacts/README.md || true
          cp -f cache.json artifacts/cache.json || true
          ls -1 *.png 2>/dev/null | xargs -I {} cp -f "{}" artifacts/ || true

          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          echo "📦 artifacts 目录内容:"
          ls -lah artifacts || true
          echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        env:
          XSERVER_EMAIL: ${{ secrets.XSERVER_EMAIL }}
          XSERVER_PASSWORD: ${{ secrets.XSERVER_PASSWORD }}
          XSERVER_VPS_ID: ${{ secrets.XSERVER_VPS_ID }}
          PROXY_SERVER: ${{ secrets.PROXY_SERVER }}
          CAPTCHA_API_URL: ${{ secrets.CAPTCHA_API_URL }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          # 你的脚本里会强制 headless=False，这里只是避免误会
          USE_HEADLESS: "false"
          WAIT_TIMEOUT: "30000"

      - name: Upload artifacts (logs/screenshots)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: xserver-renew-artifacts
          path: artifacts
          if-no-files-found: ignore
          retention-days: 14

      - name: Commit updated README/cache (optional)
        # 只有在文件变化时才提交
        if: always()
        shell: bash
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

          git add README.md cache.json renewal.log || true

          if git diff --cached --quiet; then
            echo "ℹ️ 没有变更需要提交"
            exit 0
          fi

          git commit -m "chore: auto renew status update" || true
          git push || true
