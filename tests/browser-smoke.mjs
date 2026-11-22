import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const pageErrors = []
page.on('pageerror', error => pageErrors.push(error.message))

try {
  await page.goto(process.env.WORKBENCH_URL || 'http://127.0.0.1:5173/', { waitUntil: 'networkidle' })
  assert.equal(await page.locator('h1').innerText(), '监测总览')
  const hasData = await page.getByText('导入测试数据以开始监测').count() === 0
  if (hasData) assert.equal(await page.locator('.metric-value').count(), 4)
  else assert.equal(await page.getByText('导入测试数据以开始监测').count(), 1)
  assert.equal(await page.locator('body').evaluate(element => element.scrollWidth), 1440)
  await page.screenshot({ path: `${process.env.TEMP}/workbench-desktop.png`, fullPage: true })

  await page.setViewportSize({ width: 390, height: 844 })
  assert.equal(await page.locator('body').evaluate(element => element.scrollWidth), 390)
  assert.equal(await page.getByRole('navigation').isVisible(), true)
  await page.getByRole('button', { name: '任务记录' }).click()
  assert.equal(await page.locator('h1').innerText(), '任务记录')
  if (hasData) {
    await page.getByLabel('平台').selectOption('ChatGPT')
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 20, null, { timeout: 5000 })
    await page.getByRole('button', { name: '下一页' }).click()
    await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 20 && document.body.innerText.includes('第 2 / 3 页'), null, { timeout: 5000 })
  }
  await page.getByRole('button', { name: '异常巡检' }).click()
  assert.equal(await page.locator('h1').innerText(), '异常巡检')
  if (hasData) {
    await page.getByRole('button', { name: '运行巡检' }).click()
    await page.locator('.inspection-results').waitFor({ state: 'visible', timeout: 5000 })
    const snapshotIssue = page.locator('tr').filter({ hasText: '统计快照不一致' }).first()
    if (await snapshotIssue.count()) assert.equal(await snapshotIssue.locator('button').count(), 0)
  }
  await page.screenshot({ path: `${process.env.TEMP}/workbench-mobile.png`, fullPage: true })
  assert.deepEqual(pageErrors, [])
  console.log('Browser smoke passed at 1440px and 390px; no page errors or horizontal overflow.')
} finally {
  await browser.close()
}
