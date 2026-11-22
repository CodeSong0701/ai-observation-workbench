import assert from 'node:assert/strict'
import { mkdir, rename } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { chromium } from 'playwright'

const root = process.cwd()
const inputZip = process.env.DEMO_DATA_ZIP || 'D:/郑州黑马/AI全栈笔试_模拟数据包.zip'
const output = resolve(process.env.DEMO_VIDEO_OUT || join(root, 'artifacts', '声量观测台演示.webm'))
const pace = Number(process.env.DEMO_PACE || 1)
const width = 1440
const height = 900

await mkdir(dirname(output), { recursive: true })
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({
  viewport: { width, height },
  recordVideo: { dir: dirname(output), size: { width, height } },
})
const page = await context.newPage()
page.setDefaultTimeout(15_000)
const errors = []
page.on('pageerror', error => errors.push(error.message))

async function caption(title, narration, milliseconds) {
  await page.evaluate(({ title, narration }) => {
    let host = document.getElementById('demo-caption-host')
    if (!host) {
      host = document.createElement('div')
      host.id = 'demo-caption-host'
      Object.assign(host.style, { position: 'fixed', inset: 'auto 0 0', zIndex: '2147483647', pointerEvents: 'none' })
      document.body.append(host)
      host.attachShadow({ mode: 'open' })
    }
    host.shadowRoot.innerHTML = `<style>
      section{margin:0 34px 24px;padding:15px 20px;background:rgba(21,38,33,.94);color:#fff;border-left:4px solid #62b493;border-radius:4px;font:14px/1.55 'Noto Sans SC',sans-serif;box-shadow:0 4px 20px #15262133}
      b{display:block;color:#8ed5b7;font:600 12px/1.4 'DM Mono',monospace;margin-bottom:5px}
    </style><section><b>${title}</b>${narration}</section>`
  }, { title, narration })
  await page.waitForTimeout(milliseconds * pace)
}

try {
  await page.goto(process.env.DEMO_URL || 'http://127.0.0.1:5174/', { waitUntil: 'networkidle' })
  assert.equal(await page.locator('h1').innerText(), '监测总览')

  await caption('01 / 导入模拟数据', '选择题目提供的 ZIP。导入会校验各数据段、业务键、引用关系和 manifest 记录数。', 38_000)
  await page.getByLabel('导入策略').selectOption('replace')
  page.once('dialog', dialog => dialog.accept())
  await page.locator('input[type=file]').setInputFiles(inputZip)
  await page.getByText(/已替换当前数据集/).waitFor({ timeout: 30_000 })
  await page.locator('.metric-value').first().getByText('144').waitFor({ timeout: 10_000 })

  await caption('02 / 总览和筛选', '144 条逻辑任务来自 12 个资产、3 个平台和 4 个业务日期。完成量只计算通过证据校验的结果。', 42_000)
  await page.getByLabel('品牌').selectOption('B001')
  await page.getByLabel('平台').selectOption('ChatGPT')
  await page.getByLabel('业务日期').selectOption('2026-10-10')
  await page.locator('.metric-grid').waitFor({ state: 'visible' })

  await caption('03 / 原始回答和证据', '详情同时呈现业务键、用户提问、原始回答、来源链接、结构化结论和 attempt 历史。', 42_000)
  await page.getByRole('button', { name: '任务记录' }).click()
  const analysisKey = 'B001|ChatGPT|2026-10-10'
  await page.locator('tr.click-row').filter({ hasText: analysisKey }).click()
  await page.locator('.drawer').waitFor({ state: 'visible' })
  await page.getByText('原始回答', { exact: true }).waitFor()

  await caption('04 / Mock 重新解析', '触发一次后端异步解析。结果明确标记为 MOCK，证据仍必须能从当前原始回答中定位。', 42_000)
  await page.getByRole('button', { name: /Mock 重新解析/ }).click()
  await page.getByText(/重新解析完成/).waitFor({ timeout: 20_000 })
  await page.locator('.drawer .close-button').click()

  await caption('05 / 巡检和异常分类', '巡检识别缺失、采集或分析失败、超时、逾期、重复执行、证据错误和统计快照差异。', 44_000)
  await page.getByLabel('品牌').selectOption('')
  await page.getByLabel('平台').selectOption('')
  await page.getByLabel('业务日期').selectOption('')
  await page.getByRole('button', { name: '异常巡检' }).click()
  await page.getByRole('button', { name: '运行巡检' }).click()
  await page.locator('.inspection-results').waitFor({ state: 'visible' })
  await page.getByText('统计快照不一致').first().waitFor()

  await caption('06 / 离线补采', '针对缺失任务从本地 recovery fixture 补采。应用不访问 ChatGPT、Gemini 或 Perplexity。', 42_000)
  await page.getByRole('button', { name: '任务记录' }).click()
  await page.getByLabel('产品').selectOption('P001')
  await page.getByLabel('平台').selectOption('ChatGPT')
  await page.getByLabel('业务日期').selectOption('2026-10-10')
  const recoveryKey = 'P001|ChatGPT|2026-10-10'
  await page.locator('tr.click-row').filter({ hasText: recoveryKey }).click()
  await page.getByRole('button', { name: '模拟补采' }).click()
  await page.getByText('补采任务已写入，状态已持久化').waitFor({ timeout: 20_000 })

  await caption('07 / 刷新确认持久化', '重新读取详情，确认补采后的回答、Mock 分析和执行历史仍保存在 SQLite。', 42_000)
  await page.locator('.drawer .close-button').click()
  await page.locator('.drawer').waitFor({ state: 'detached' })
  await page.getByRole('button', { name: '刷新数据' }).click()
  await page.getByText('有效完成', { exact: true }).waitFor({ timeout: 10_000 })

  await caption('08 / 扩展边界', '当前采用 FastAPI、SQLite 和可恢复的轻量 Runner。对外部署前再启用认证与租户隔离；规模增长后接入队列和 PostgreSQL。', 42_000)
  assert.deepEqual(errors, [])
} finally {
  await context.close()
  await browser.close()
}

const videoPath = await page.video().path()
await rename(videoPath, output)
console.log(`Recorded demo video: ${output}`)
