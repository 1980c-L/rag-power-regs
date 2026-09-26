import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { service } from '../services';
import { ApiError } from '../services/apiService';
import { JOB_STATE_LABELS, ITEM_STATUS_LABELS, MAX_QUESTIONS, parseCsv, parsePastedText, parseTxtFile } from '../app/batchParse';
import type { BatchJob, BatchJobSummary, CorpusId, GenerateMode, MetaResponse } from '../types';

/**
 * 批量提问 + 报告导出（47 号方案阶段 A 的页面侧）。
 *
 * 页面只做四件事：收集输入 → 显示运行前摘要并二次确认 → 轮询进度 → 触发导出。
 * **执行语义一律在服务端**（batch_jobs.py）：1–20 条硬上限、串行、每题最多 1 次请求、
 * 零自动重试、系统性错误停手、取消只停后续。页面不复制任何一条，也不许把状态说成别的。
 *
 * 三条不许说谎的地方：
 *  1. 状态词与接口一一对应：完成 / 未找到资料依据（拒答）/ 失败 / 未执行；
 *  2. 批量结果里**没有** Hit@K / MRR —— 这里不出现任何"正确率"字样；
 *  3. mock 模式（没有服务端）如实显示不可用，不假装能跑。
 */

const STORAGE_KEY = 'rag.batch_job_id';
const FINAL_STATES = ['COMPLETED', 'CANCELLED', 'STOPPED_ON_ERROR'];

interface Props {
  meta: MetaResponse | null;
  /** 单题正在检索/生成时把批量入口也锁上，避免同屏两个运行中的东西互相干扰 */
  locked: boolean;
}

export function BatchPanel({ meta, locked }: Props) {
  const cap = meta?.batch ?? null;
  const available = Boolean(cap) && Boolean(service.createBatchJob);

  const [raw, setRaw] = useState('');
  const [parsed, setParsed] = useState(() => parsePastedText(''));
  const [corpusId, setCorpusId] = useState<CorpusId>('rag_learning');
  const [mode, setMode] = useState<GenerateMode>('retrieve_only');
  const [topK, setTopK] = useState(3);
  const [job, setJob] = useState<BatchJob | null>(null);
  const [summary, setSummary] = useState<BatchJobSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openSeq, setOpenSeq] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const questions = parsed.questions;
  const plannedCalls = mode === 'generate' ? questions.length : 0;
  const running = job !== null && !FINAL_STATES.includes(job.state);
  const jobId = job?.job_id ?? null;
  const jobState = job?.state ?? null;
  const blocked = locked || busy;

  const corpora = meta?.corpora ?? [];
  const topKChoices = useMemo(() => {
    const range = cap?.top_k_range;
    if (range && range.length === 2) {
      const out: number[] = [];
      for (let n = range[0]; n <= range[1]; n += 1) out.push(n);
      return out;
    }
    return meta?.top_k_choices ?? [1, 2, 3, 4, 5];
  }, [cap, meta]);

  const refresh = useCallback(async (id: string) => {
    if (!service.getBatchJob) return;
    try {
      const res = await service.getBatchJob(id);
      setJob(res.job);
      setSummary(res.summary);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      // 任务不在内存里（服务重启 = 第一版明确不续跑）：如实说明并清掉本地记录，绝不"假装还能续"
      setError(`读取批量任务失败：${msg}`);
      window.localStorage.removeItem(STORAGE_KEY);
      setJob(null);
      setSummary(null);
    }
  }, []);

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved && available) void refresh(saved);
  }, [available, refresh]);

  useEffect(() => {
    if (!jobId || !jobState || FINAL_STATES.includes(jobState)) return undefined;
    const timer = window.setInterval(() => { void refresh(jobId); }, 800);
    return () => window.clearInterval(timer);
  }, [jobId, jobState, refresh]);

  const onRawChange = useCallback((text: string) => {
    setRaw(text);
    const result = parsePastedText(text);
    setParsed(result);
    // 输入即校验：超过 20 条 / 单条过长在"还没点开始"时就说清楚（服务端仍会再拒一次）
    setError(result.error);
    setNotice(null);
  }, []);

  const loadFile = useCallback(async (files: FileList | null) => {
    if (!files || !files.length) return;
    const file = files[0];
    setError(null);
    setNotice(null);
    const text = await file.text();
    const isCsv = file.name.toLowerCase().endsWith('.csv');
    const result = isCsv ? parseCsv(text) : parseTxtFile(text);
    if (result.error) {
      // 解析失败时**不改动**输入框与已解析结果：让"输入框内容 = 将要执行的问题"始终保持一致，
      // 只把原因说清楚（写进去却又不执行，是更难理解的失败方式）
      setError(`${file.name}：${result.error}`);
      if (fileRef.current) fileRef.current.value = '';
      return;
    }
    const joined = result.questions.join('\n');
    setRaw(joined);
    setParsed(parsePastedText(joined));
    setNotice(`已从 ${file.name} 读取 ${result.questions.length} 道问题`
      + (isCsv ? '（question 列）。' : '（一行一个问题）。'));
    if (fileRef.current) fileRef.current.value = '';
  }, []);

  const start = useCallback(() => {
    if (!available || !service.createBatchJob) return;
    const now = parsePastedText(raw);
    setParsed(now);
    setError(null);
    setNotice(null);
    if (now.error) {
      setError(now.error);
      return;
    }
    const planned = mode === 'generate' ? now.questions.length : 0;
    if (mode === 'generate') {
      // 二次确认：确认值必须与服务端重新计算的 N 完全一致（服务端还会再核一遍）
      const corpusLabel = corpora.find((c) => c.corpus_id === corpusId)?.name ?? corpusId;
      const ok = window.confirm(
        `即将执行 ${now.questions.length} 道问题：知识库「${corpusLabel}」、Top-K ${topK}、` +
        `模式「检索并生成」。\n预计最多调用模型 ${planned} 次（串行执行、每题最多 1 次、不自动重试）。\n确认开始吗？`,
      );
      if (!ok) return;
    }
    setBusy(true);
    void (async () => {
      try {
        const res = await service.createBatchJob!({
          corpus_id: corpusId,
          mode,
          top_k: topK,
          questions: now.questions,
          confirm_calls: planned,
        });
        setJob(res.job);
        setSummary(res.summary);
        window.localStorage.setItem(STORAGE_KEY, res.job.job_id);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    })();
  }, [available, raw, mode, corpora, corpusId, topK]);

  const cancel = useCallback(() => {
    if (!job || !service.cancelBatchJob) return;
    setBusy(true);
    void (async () => {
      try {
        const res = await service.cancelBatchJob!(job.job_id);
        setJob(res.job);
        setSummary(res.summary);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    })();
  }, [job]);

  const exportHref = (format: 'csv' | 'docx'): string =>
    (job && service.batchExportUrl ? service.batchExportUrl(job.job_id, format) : '');

  return (
    <section className="card batch-card" id="batch-panel">
      <h2 className="aside-card__title">批量提问（多问题 + 报告导出）</h2>
      <p className="muted small">
        这是<strong>独立入口</strong>，不影响上面的单题流程。一次 {cap?.min_questions ?? 1}–
        {cap?.max_questions ?? MAX_QUESTIONS} 条；「仅检索」模式<strong>不调用模型</strong>；
        「检索并生成」模式<strong>串行</strong>执行、每题最多 1 次请求、
        <strong>不自动重试</strong>，遇到认证/网络/服务端错误会停止启动后续问题。
      </p>
      <p className="muted small" id="batch-scope">
        导出只读取本次已保存的结果：不重新检索、不重新生成、不新增模型调用；
        批量结果<strong>不包含 Hit@K / MRR</strong>，也不把它当作答案正确率。
        「完成」「未找到资料依据（拒答）」「失败」「未执行」四种状态互不冒充。
      </p>

      {!available && (
        <p className="muted small" id="batch-unavailable">
          {meta?.data_source === 'api'
            ? '本机 API 没有提供批量能力信息（可能是旧版本服务），批量提问不可用。'
            : '本地示例数据模式（mock）没有服务端，批量提问不可用：请先用 python api_v3.py 启动本机接口。'}
        </p>
      )}

      {available && (
        <>
          <div className="field">
            <span className="field__label">问题（一行一个问题；也可以导入 .txt / .csv）</span>
            <textarea
              id="batch-questions"
              className="userlib-textarea batch-textarea"
              rows={6}
              placeholder={'RAG 评估三元组包含哪三个维度？\n装设接地线的顺序是什么？'}
              value={raw}
              disabled={blocked || running}
              onChange={(e) => onRawChange(e.target.value)}
            />
            <div className="batch-row">
              <label className="btn batch-file">
                导入 TXT / CSV
                <input
                  ref={fileRef}
                  id="batch-file"
                  type="file"
                  accept=".txt,.csv"
                  disabled={blocked || running}
                  onChange={(e) => void loadFile(e.target.files)}
                />
              </label>
              <span className="muted small" id="batch-count">
                已读取 {questions.length} 道问题（上限 {cap?.max_questions ?? MAX_QUESTIONS}，
                单条 ≤ {cap?.max_question_chars ?? 500} 字符）
              </span>
            </div>
          </div>

          <div className="batch-grid">
            <label className="field">
              <span className="field__label">知识库</span>
              <select
                id="batch-corpus"
                className="select"
                value={corpusId}
                disabled={blocked || running}
                onChange={(e) => setCorpusId(e.target.value as CorpusId)}
              >
                {corpora.map((c) => (
                  <option key={c.corpus_id} value={c.corpus_id}>{c.name}</option>
                ))}
                {meta?.user_library?.available && (
                  <option value="user_library">{meta.user_library.name}</option>
                )}
              </select>
            </label>
            <label className="field">
              <span className="field__label">模式</span>
              <select
                id="batch-mode"
                className="select"
                value={mode}
                disabled={blocked || running}
                onChange={(e) => setMode(e.target.value as GenerateMode)}
              >
                <option value="retrieve_only">仅检索（0 次模型请求）</option>
                <option value="generate">检索并生成（每题最多 1 次）</option>
              </select>
            </label>
            <label className="field">
              <span className="field__label">Top-K</span>
              <select
                id="batch-top-k"
                className="select"
                value={topK}
                disabled={blocked || running}
                onChange={(e) => setTopK(Number(e.target.value))}
              >
                {topKChoices.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </div>

          <p className="batch-summary small" id="batch-summary">
            运行前摘要：知识库{' '}
            <strong>{corpora.find((c) => c.corpus_id === corpusId)?.name ?? corpusId}</strong>
            {corpusId === 'user_library' ? '（个人资料，不参与评测）' : ''} · 模式{' '}
            <strong>{mode === 'generate' ? '检索并生成' : '仅检索'}</strong> · 问题数{' '}
            <strong>{questions.length}</strong> · Top-K <strong>{topK}</strong> · 预计最多模型请求{' '}
            <strong>{plannedCalls}</strong> 次
            {mode === 'generate' && questions.length > 0 ? '（点击开始后会再弹一次确认）' : ''}
          </p>

          <div className="batch-row">
            <button
              type="button"
              id="batch-start"
              className="btn btn--primary"
              disabled={blocked || running || questions.length === 0}
              onClick={start}
            >
              {running ? '批量任务运行中…' : '开始批量提问'}
            </button>
            <button
              type="button"
              id="batch-cancel"
              className="btn"
              disabled={!running || busy}
              onClick={cancel}
            >
              停止（当前这条跑完就停）
            </button>
          </div>

          {error && <p className="userlib-msg userlib-msg--err" id="batch-error">{error}</p>}
          {notice && <p className="userlib-msg userlib-msg--ok" id="batch-notice">{notice}</p>}

          {job && summary && (
            <div id="batch-result">
              <p className="batch-summary small">
                任务 <code>{job.job_id.slice(0, 8)}</code> · 状态{' '}
                <strong id="batch-state">{JOB_STATE_LABELS[job.state] ?? job.state}</strong> · 已完成{' '}
                <strong id="batch-progress">{summary.finished} / {summary.total}</strong>
              </p>
              <p className="muted small">
                知识库：{job.corpus_name} · 模式：{job.mode === 'generate' ? '检索并生成' : '仅检索'} ·
                Top-K {job.top_k} · 计划模型请求 {job.planned_calls} 次 · 实际启动 {job.started_calls} 次 ·
                自动重试 {job.retries} 次
                {job.mode === 'generate'
                  ? (job.llm_loopback ? '（生成目标是本机回环地址，未调用真实模型供应商）'
                    : '（生成目标是外部模型服务）')
                  : ''}
              </p>
              {job.stop_reason && (
                <p className="userlib-msg userlib-msg--err" id="batch-stop-reason">{job.stop_reason}</p>
              )}

              <ul className="batch-stats small" id="batch-stats">
                <li>完成：<strong>{summary.done}</strong></li>
                <li>未找到资料依据（拒答）：<strong>{summary.refused}</strong></li>
                <li>失败：<strong>{summary.failed}</strong></li>
                <li>未执行：<strong>{summary.not_executed}</strong></li>
              </ul>

              {running ? (
                <p className="muted small" id="batch-export-hint">
                  任务结束后可导出 DOCX / CSV（导出只读取已保存的结果，不会重新检索或生成）。
                </p>
              ) : (
                <div className="batch-row">
                  <a id="batch-export-docx" className="btn" href={exportHref('docx')} download>
                    导出 DOCX（报告）
                  </a>
                  <a id="batch-export-csv" className="btn" href={exportHref('csv')} download>
                    导出 CSV（明细）
                  </a>
                </div>
              )}
              <p className="muted small">
                导出不新增任何模型调用；批量结果里<strong>不包含 Hit@K / MRR</strong>，
                也不把它当作答案正确率。「完成」只表示这条跑完了，「未找到资料依据」是模型按约束明确拒答，
                「失败」是模型或网络错误，「未执行」是被取消或提前停止留下的 —— 三者不互相冒充。
              </p>

              <ol className="batch-items" id="batch-items">
                {job.items.map((item) => (
                  <li key={item.sequence} className={`batch-item batch-item--${item.status}`}>
                    <button
                      type="button"
                      className="batch-item__head"
                      onClick={() => setOpenSeq(openSeq === item.sequence ? null : item.sequence)}
                    >
                      <span className="batch-item__seq">{item.sequence}</span>
                      <span className="batch-item__q">{item.question}</span>
                      <span className="batch-item__status">
                        {ITEM_STATUS_LABELS[item.status] ?? item.status}
                      </span>
                    </button>
                    {openSeq === item.sequence && (
                      <div className="batch-item__body">
                        <p className="muted small">
                          检索命中 {item.hits.length} 段 · 同节补充 {item.neighbors_count} 段 ·
                          检索 {item.elapsed_ms.retrieve ?? '-'} ms · 生成 {item.elapsed_ms.generate ?? '-'} ms
                        </p>
                        {item.answer
                          ? <p className="batch-item__answer">{item.answer}</p>
                          : <p className="muted small">（没有回答文本）</p>}
                        {item.refs.length > 0 && (
                          <p className="muted small">
                            引用（去重 {item.refs.length} 个 / 正文出现 {item.ref_occurrences} 次）：
                            {item.refs.map((r) => r.id).join('、')}
                          </p>
                        )}
                        {item.error && (
                          <p className="userlib-msg userlib-msg--err">{item.error}</p>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </>
      )}
    </section>
  );
}
