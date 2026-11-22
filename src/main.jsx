import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  Activity, AlertTriangle, ArrowDownToLine, ArrowUpRight, Bot, Check, ChevronDown,
  CircleHelp, Clock3, Database, FileJson2, Filter, Layers3, LoaderCircle, Play,
  RefreshCw, Search, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import './styles.css'

const api = async (path, options = {}) => {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`)
  return payload
}

function App() {
  const [view, setView] = useState('overview')
  const [overview, setOverview] = useState(null)
  const [tasks, setTasks] = useState({ items: [], total: 0 })
  const [filters, setFilters] = useState({ brand_id: '', asset_id: '', platform: '', business_date: '', q: '' })
  const [page, setPage] = useState(1)
  const [importMode, setImportMode] = useState('merge')
  const [dataset, setDataset] = useState(null)
  const [selected, setSelected] = useState(null)
  const [inspection, setInspection] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const query = useMemo(() => new URLSearchParams(Object.entries(filters).filter(([, value]) => value)), [filters])
  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      const overviewQuery = new URLSearchParams(Object.entries(filters).filter(([key, value]) => key !== 'q' && value))
      const [summary, taskPage, datasetState] = await Promise.all([
        api(`/overview?${overviewQuery}`), api(`/tasks?${query}&page=${page}&page_size=20`), api('/dataset'),
      ])
      setOverview(summary)
      setTasks(taskPage)
      setDataset(datasetState)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [query.toString(), page])

  const updateFilter = (key, value) => {
    setPage(1)
    setFilters(current => ({ ...current, [key]: value, ...(key === 'brand_id' ? { asset_id: '' } : {}) }))
  }

  const importFile = async (file) => {
    if (!file) return
    if (importMode === 'replace' && !window.confirm('替换将清除当前任务、结果和执行记录，并取消未完成的后台任务。继续吗？')) return
    setBusy('import')
    setNotice('')
    setError('')
    try {
      const isArchive = file.name.toLowerCase().endsWith('.zip')
      const result = isArchive
        ? await api(`/seed/import-archive?mode=${importMode}`, { method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: await file.arrayBuffer() })
        : await api(`/seed/import?mode=${importMode}`, { method: 'POST', body: JSON.stringify(JSON.parse(await file.text())) })
      setPage(1)
      setNotice(`${importMode === 'replace' ? '已替换当前数据集' : '已合并导入'}，本批写入 ${result.imported} 条记录`)
      await refresh()
    } catch (err) {
      setError(err instanceof SyntaxError ? 'JSON 文件无法解析' : err.message)
    } finally {
      setBusy('')
    }
  }

  const openTask = async (task) => {
    try {
      setSelected(await api(`/tasks/${encodeURIComponent(task.task_key)}`))
    } catch (err) { setError(err.message) }
  }

  const runInspection = async () => {
    setBusy('inspect')
    setError('')
    try {
      const body = Object.fromEntries(Object.entries(filters).filter(([key, value]) => key !== 'q' && value))
      setInspection(await api('/inspections', { method: 'POST', body: JSON.stringify(body) }))
      setView('inspections')
    } catch (err) { setError(err.message) }
    finally { setBusy('') }
  }

  const reanalyze = async (mode) => {
    if (!selected) return
    setBusy('analyze')
    setError('')
    try {
      const accepted = await api(`/reanalyze?task_key=${encodeURIComponent(selected.task_key)}`, {
        method: 'POST', body: JSON.stringify({ mode }),
      })
      let run = accepted
      for (let i = 0; i < 24 && ['queued', 'running'].includes(run.status); i++) {
        await new Promise(resolve => setTimeout(resolve, 500))
        run = await api(`/runs/${accepted.run_id}`)
      }
      if (run.status !== 'succeeded') throw new Error(run.error || '解析未完成')
      setSelected(await api(`/tasks/${encodeURIComponent(selected.task_key)}`))
      setNotice(`重新解析完成 · ${mode.toUpperCase()}`)
      await refresh()
    } catch (err) { setError(err.message) }
    finally { setBusy('') }
  }

  const retryTask = async (taskKey) => {
    setBusy(`retry:${taskKey}`)
    try {
      await api(`/retry?task_key=${encodeURIComponent(taskKey)}`, { method: 'POST' })
      setNotice('补采任务已写入，状态已持久化')
      await refresh()
      if (selected?.task_key === taskKey) setSelected(await api(`/tasks/${encodeURIComponent(taskKey)}`))
    } catch (err) { setError(err.message) }
    finally { setBusy('') }
  }

  const title = view === 'overview' ? '监测总览' : view === 'tasks' ? '任务记录' : '异常巡检'

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-mark"><span className="mark-glyph"><Activity size={18} /></span><div><b>观测台</b><small>AI ANSWER MONITOR</small></div></div>
      <div className="workspace-label">工作区</div>
      <nav aria-label="主导航">
        <button aria-label="总览" title="总览" className={view === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setView('overview')}><Layers3 size={17} />总览</button>
        <button aria-label="任务记录" title="任务记录" className={view === 'tasks' ? 'nav-item active' : 'nav-item'} onClick={() => setView('tasks')}><Database size={17} />任务记录</button>
        <button aria-label="异常巡检" title="异常巡检" className={view === 'inspections' ? 'nav-item active' : 'nav-item'} onClick={() => setView('inspections')}><ShieldCheck size={17} />异常巡检</button>
      </nav>
      <div className="sidebar-bottom">
        <div className="mode-indicator"><span className="status-dot" />{overview?.expected ? `已加载 ${overview.expected} 条应有任务` : '数据源就绪后可导入'}</div>
        <div className="sidebar-foot"><CircleHelp size={15} />本地演示环境 <span>v1.0</span></div>
      </div>
    </aside>

    <main className="main-area">
      <header className="topbar">
        <div className="breadcrumbs"><span>工作台</span><span className="crumb-sep">/</span><strong>{title}</strong></div>
        <div className="top-actions">
          <span className="mode-pill"><span />MOCK / LIVE</span>
          <select aria-label="导入策略" className="import-mode" value={importMode} onChange={e => setImportMode(e.target.value)}>
            <option value="merge">合并导入</option><option value="replace">替换数据集</option>
          </select>
          <label className={`button primary ${busy === 'import' ? 'disabled' : ''}`} title="选择 seed_all.json 导入测试数据">
            {busy === 'import' ? <LoaderCircle className="spin" size={16} /> : <ArrowDownToLine size={16} />}
            加载测试数据<input type="file" accept=".json,.zip,application/json,application/zip" hidden onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; importFile(file) }} />
          </label>
        </div>
      </header>

      <section className="page-content">
        <div className="page-heading">
          <div><div className="eyebrow">AI ANSWER INTELLIGENCE</div><h1>{title}</h1><p>{view === 'overview' ? '跨平台回答采集、分析与任务健康状态' : view === 'tasks' ? '按业务键追踪采集记录、分析结果与执行历史' : '对照应有任务与有效结果，发现缺失并执行恢复'}</p></div>
          <button className="button subtle" onClick={refresh} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''} />刷新数据</button>
        </div>
        <div className="data-provenance"><CircleHelp size={14} />合成演示数据；平台名称仅为维度标签，不代表真实平台调用。{dataset?.latest_import && <span>{dataset.latest_import.mode === 'existing' ? '现有数据记录于' : `最近${dataset.latest_import.mode === 'replace' ? '替换' : '合并'}`} {new Date(dataset.latest_import.created_at).toLocaleString('zh-CN')}</span>}</div>

        {notice && <div className="notice success"><Check size={16} />{notice}<button onClick={() => setNotice('')} aria-label="关闭"><X size={15} /></button></div>}
        {error && <div className="notice error"><AlertTriangle size={16} />{error}<button onClick={() => setError('')} aria-label="关闭"><X size={15} /></button></div>}

        <div className="filter-row">
          <div className="filter-caption"><Filter size={15} />筛选范围</div>
          <select aria-label="品牌" value={filters.brand_id} onChange={e => updateFilter('brand_id', e.target.value)}>
            <option value="">全部品牌</option>{(overview?.brands || []).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
          <select aria-label="产品" value={filters.asset_id} onChange={e => updateFilter('asset_id', e.target.value)}>
            <option value="">全部产品</option>{(overview?.assets || []).filter(asset => !filters.brand_id || asset.brand_id === filters.brand_id).map(asset => <option key={asset.asset_id} value={asset.asset_id}>{asset.name}</option>)}
          </select>
          <select aria-label="平台" value={filters.platform} onChange={e => updateFilter('platform', e.target.value)}>
            <option value="">全部平台</option>{(overview?.platforms || []).map(name => <option key={name}>{name}</option>)}
          </select>
          <select aria-label="业务日期" value={filters.business_date} onChange={e => updateFilter('business_date', e.target.value)}>
            <option value="">全部日期</option>{(overview?.dates || []).map(day => <option key={day}>{day}</option>)}
          </select>
          {view === 'tasks' && <label className="search-box"><Search size={15} /><input placeholder="搜索业务键或提问" value={filters.q} onChange={e => updateFilter('q', e.target.value)} /></label>}
          <button className="button outline action-filter" onClick={runInspection} disabled={busy === 'inspect'}>{busy === 'inspect' ? <LoaderCircle className="spin" size={15} /> : <Play size={14} />}运行巡检</button>
        </div>

        {view === 'overview' && <Overview summary={overview} tasks={tasks.items} openTask={openTask} onRetry={retryTask} busy={busy} onGoTasks={() => setView('tasks')} />}
        {view === 'tasks' && <TaskTable items={tasks.items} openTask={openTask} total={tasks.total} page={tasks.page} pageSize={tasks.page_size} onPage={setPage} />}
        {view === 'inspections' && <InspectionPanel result={inspection} tasks={tasks.items} openTask={openTask} onRetry={retryTask} busy={busy} />}
      </section>
    </main>
    {selected && <TaskDrawer task={selected} close={() => setSelected(null)} reanalyze={reanalyze} retry={retryTask} busy={busy} />}
  </div>
}

function Overview({ summary, tasks, openTask, onRetry, busy, onGoTasks }) {
  if (!summary || summary.expected === 0) return <EmptyState />
  const recentIssues = tasks.filter(task => !task.valid).slice(0, 5)
  return <>
    <div className="metric-grid">
      <Metric label="应有任务" value={summary.expected} note="按资产 × 平台 × 日期" icon={<Database size={17} />} tone="ink" />
      <Metric label="有效完成" value={summary.valid_complete} note={`${summary.completion_rate}% 完成率`} icon={<Check size={17} />} tone="green" />
      <Metric label="异常任务" value={summary.exceptions} note={`${summary.statistic_diffs || 0} 条统计快照差异`} icon={<AlertTriangle size={17} />} tone="red" />
      <Metric label="品牌提及率" value={`${summary.mention_rate}%`} note="有效分析结果中的提及占比" icon={<Sparkles size={17} />} tone="teal" />
    </div>
    <section className="data-section">
      <div className="section-head"><div><h2>需要关注</h2><p>未达到有效完成条件的任务</p></div><button className="text-button" onClick={onGoTasks}>查看全部 <ArrowUpRight size={14} /></button></div>
      {recentIssues.length ? <TaskTable compact items={recentIssues} openTask={openTask} onRetry={onRetry} busy={busy} /> : <div className="all-clear"><Check size={18} />当前筛选范围内没有未完成任务</div>}
    </section>
    <div className="footnote"><Clock3 size={14} />统计仅计有效业务任务，重复 attempt 不增加完成量</div>
  </>
}

function Metric({ label, value, note, icon, tone }) {
  return <article className="metric"><div className={`metric-icon ${tone}`}>{icon}</div><div className="metric-label">{label}</div><div className="metric-value">{value}</div><div className="metric-note">{note}</div></article>
}

function EmptyState() {
  return <div className="empty-state"><div className="empty-icon"><FileJson2 size={24} /></div><div className="eyebrow">等待数据包</div><h2>导入测试数据以开始监测</h2><p>选择题目提供的 seed_all.json，工作台会加载任务、采集回答、分析结果和巡检输入。</p><div className="empty-meta"><span><Check size={14} />支持重复导入</span><span><ShieldCheck size={14} />校验后持久化到本地</span></div></div>
}

function TaskTable({ items, openTask, total, page = 1, pageSize = 20, onPage, compact = false, onRetry, busy }) {
  if (!items?.length) return compact ? <div className="table-empty">暂无需要关注的任务</div> : <EmptyState />
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  return <div className="table-wrap"><table><thead><tr><th>业务任务</th><th>平台 / 日期</th><th>最近状态</th><th>执行次数</th>{!compact && <th>分析摘要</th>}{onRetry && <th className="th-action">操作</th>}</tr></thead><tbody>
    {items.map(task => <tr key={task.task_key} onClick={() => openTask(task)} className="click-row">
      <td><div className="task-key">{task.task_key}</div><div className="task-query">{task.user_query || '未提供用户提问'}</div></td>
      <td><div>{task.platform || '—'}</div><div className="muted small">{task.business_date || '—'}</div></td>
      <td><Status status={task.status} /></td><td>{task.attempt_count}</td>
      {!compact && <td className="summary-cell">{task.summary || task.validation_error || '尚无有效分析'}</td>}
      {onRetry && <td className="action-cell"><button className="icon-button" title="对该任务尝试补采" onClick={e => { e.stopPropagation(); onRetry(task.task_key) }} disabled={busy === `retry:${task.task_key}`}><RefreshCw size={15} className={busy === `retry:${task.task_key}` ? 'spin' : ''} /></button></td>}
    </tr>)}</tbody></table>{!compact && <div className="table-footer"><span>第 {page} / {pageCount} 页 · 共 {total} 条</span>{onPage && <div className="pagination"><button className="button subtle" onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1}>上一页</button><button className="button subtle" onClick={() => onPage(Math.min(pageCount, page + 1))} disabled={page >= pageCount}>下一页</button></div>}</div>}</div>
}

function Status({ status }) {
  const tone = status === '有效完成' ? 'ok' : status === '处理中' ? 'working' : status === '待分析' ? 'pending' : 'issue'
  return <span className={`status-badge ${tone}`}><i />{status}</span>
}

function InspectionPanel({ result, tasks, openTask, onRetry, busy }) {
  if (!result) return <div className="inspection-intro"><div className="intro-mark"><ShieldCheck size={24} /></div><h2>运行完整性巡检</h2><p>按当前筛选范围核对预期任务、采集记录和有效分析结果，检测缺失、失败、卡住、重复执行及证据不一致。</p><div className="rule-line"><span>应有任务集合</span><ArrowUpRight size={15} /><span>业务键去重</span><ArrowUpRight size={15} /><span>有效结果校验</span></div></div>
  return <div className="inspection-results"><div className="inspection-summary"><div><div className="eyebrow">INSPECTION RESULT</div><h2>本次巡检</h2><p>{result.created_at ? new Date(result.created_at).toLocaleString('zh-CN') : ''} · 检查 {result.checked} 条逻辑任务</p></div><div className="issue-count"><b>{result.issue_count}</b><span>异常项</span></div></div>
    {result.issues?.length ? <div className="table-wrap"><table><thead><tr><th>异常类型</th><th>业务键</th><th>问题说明</th><th>操作</th></tr></thead><tbody>{result.issues.map((issue, index) => <tr key={`${issue.task_key}-${issue.code}-${index}`}><td><span className="issue-type">{issue.title}</span></td><td>{issue.asset_id ? <button className="link-button" onClick={() => openTask({ task_key: issue.task_key })}>{issue.task_key}</button> : <span className="task-key">{issue.task_key}</span>}</td><td className="detail-copy">{issue.detail}</td><td>{issue.asset_id && <button className="icon-button" title="尝试补采" onClick={() => onRetry(issue.task_key)} disabled={busy === `retry:${issue.task_key}`}><RefreshCw size={15} /></button>}</td></tr>)}</tbody></table></div> : <div className="all-clear"><Check size={18} />巡检完成，未发现异常</div>}</div>
}

function TaskDrawer({ task, close, reanalyze, retry, busy }) {
  const detail = task.task || {}
  return <div className="drawer-backdrop" onMouseDown={e => e.target === e.currentTarget && close()}><aside className="drawer">
    <div className="drawer-top"><div><div className="eyebrow">TASK TRACE</div><h2>任务详情</h2></div><button className="icon-button close-button" onClick={close} aria-label="关闭"><X size={18} /></button></div>
    <div className="drawer-scroll"><div className="drawer-key">{task.task_key}</div><div className="detail-status"><Status status={task.status} /><span>{task.attempt_count} 次执行</span></div>
      <section className="detail-section"><h3>任务信息</h3><dl><dt>用户提问</dt><dd>{detail.user_query || '—'}</dd><dt>资产 / 平台</dt><dd>{detail.asset_id || task.asset_id || '—'} · {detail.platform || task.platform || '—'}</dd><dt>业务日期</dt><dd>{detail.business_date || task.business_date || '—'}</dd></dl></section>
      {task.failure_reason && <div className="failure-box"><AlertTriangle size={15} />{task.failure_reason}</div>}
      <section className="detail-section"><div className="section-title-line"><h3>原始回答</h3><span className="source-tag">COLLECTED</span></div><div className="raw-answer">{task.raw_answer || '暂无采集回答'}</div>{task.source_urls?.length > 0 && <div className="source-list">{task.source_urls.map(url => <a href={url} target="_blank" rel="noreferrer" key={url}>{url}</a>)}</div>}</section>
      <section className="detail-section"><div className="section-title-line"><h3>结构化分析</h3>{(task.analysis?.mode || task.analysis?.model_mode) && <span className={`mode-label ${(task.analysis.mode || task.analysis.model_mode).toLowerCase().includes('live') ? 'live' : 'mock'}`}>{(task.analysis.mode || task.analysis.model_mode).toUpperCase()}</span>}</div>{task.analysis && Object.keys(task.analysis).length ? <div className="analysis-block"><div className="analysis-summary">{task.analysis.summary || '—'}</div><div className="analysis-tags"><span>提及品牌 <b>{String(task.analysis.brand_mentioned)}</b></span><span>建议推荐 <b>{String(task.analysis.recommended)}</b></span></div><div className="evidence"><label>证据原文</label><blockquote>{task.analysis.evidence_text || '—'}</blockquote></div>{task.validation_error && <div className="validation-error"><AlertTriangle size={14} />{task.validation_error}</div>}</div> : <div className="muted">尚无分析结果</div>}</section>
      <section className="detail-section"><h3>执行历史</h3>{task.attempts?.length ? <div className="timeline">{task.attempts.map((attempt, i) => <div className="timeline-item" key={attempt.attempt_id || i}><span className="timeline-dot" /><div><b>{attempt.status || '未知状态'}</b><small>{attempt.started_at || attempt.created_at || '时间未知'} · {attempt.error || attempt.failure_reason || attempt.source || '无错误信息'}</small></div></div>)}</div> : <div className="muted">暂无执行记录</div>}</section>
    </div>
    <div className="drawer-actions"><button className="button outline" onClick={() => retry(task.task_key)} disabled={busy === `retry:${task.task_key}`}><RefreshCw size={15} />模拟补采</button><button className="button primary" onClick={() => reanalyze('mock')} disabled={busy === 'analyze' || !task.raw_answer}><Bot size={16} />{busy === 'analyze' ? '解析中…' : 'Mock 重新解析'}</button></div>
  </aside></div>
}

createRoot(document.getElementById('root')).render(<App />)
