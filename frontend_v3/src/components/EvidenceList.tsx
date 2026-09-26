import { useState } from 'react';
import { clip, sourceLine } from '../app/derive';
import type { EvidenceItem, QueryResponse, Run } from '../types';

interface Props {
  run: Run;
  response: QueryResponse | null;
}

function Row({ item, index }: { item: EvidenceItem; index: number }) {
  const [open, setOpen] = useState(false);
  const isHit = item.origin === 'hit';
  return (
    <li className={`evidence-row ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="evidence-row__head"
        aria-expanded={open}
        title={item.id}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="evidence-row__no">{index}</span>
        <span className={`chip ${isHit ? 'chip--hit' : 'chip--neighbor'}`}>
          {isHit ? '检索命中' : '同节补充'}
        </span>
        <span className="evidence-row__score">
          {isHit && item.score !== null
            ? <>BM25 {item.score.toFixed(4)}</>
            : <span className="muted">无 BM25 分</span>}
        </span>
        <span className="evidence-row__source">
          {item.source_title || item.source_id}
          {item.section && <span className="muted"> · {item.section}</span>}
        </span>
        <span className="evidence-row__clip">{clip(item.text, 72)}</span>
        <span className="evidence-row__chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="evidence-row__body">
          <p className="evidence-row__id">
            <code>{item.id}</code>
            <span className="muted">（完整 chunk id，可追溯到来源文件与段落）</span>
          </p>
          <p className="evidence-row__text">{item.text}</p>
          <p className="muted">{sourceLine(item)}</p>
          {item.source_url && (
            <p>
              <a href={item.source_url} target="_blank" rel="noreferrer">查看原文</a>
            </p>
          )}
          <p className="muted">
            {isHit
              ? 'BM25 得分只表示该段与问题在关键词上的相关程度排序，不是置信度、不是正确率；检索层没有相关性阈值。'
              : '该片段是生成阶段按「同 source_id + 同章节」补入的相邻内容，不计入检索命中，也不计入 Hit@K / MRR。'}
          </p>
        </div>
      )}
    </li>
  );
}

/** 证据片段：命中区与补充区分开；补充只在真的走到生成调用时才出现 */
export function EvidenceList({ run, response }: Props) {
  const hits = response?.hits ?? [];
  const neighbors = response?.neighbors ?? [];
  const busy = run.status === 'retrieving' || run.status === 'generating';
  const ctx = response?.context_chars ?? null;

  return (
    <section className="card evidence-card" id="evidence-card">
      <header className="panel__head">
        <h2 className="panel__title" id="evidence-title">证据片段</h2>
        <span className="count">
          {hits.length > 0
            ? `共 ${hits.length} 个检索命中${neighbors.length > 0 ? ` + ${neighbors.length} 个同节补充` : ''}`
            : '—'}
        </span>
      </header>

      {busy && <p className="muted">检索进行中…</p>}

      {!busy && hits.length === 0 && (
        <p className="muted">
          {response ? '本次没有检索命中，没有可展示的片段。' : '还没有提问。'}
        </p>
      )}

      {!busy && hits.length > 0 && (
        <>
          <p className="panel__meta">
            检索：BM25 Top-{response?.request.top_k} 命中 <strong>{hits.length}</strong> 段
            （<strong>这一段才进入 Hit@K / MRR 口径</strong>）
            {ctx ? ` · 送入生成的上下文共 ${ctx.total} 字符（命中 ${ctx.hit} + 补充 ${ctx.total - ctx.hit}，预算 ${ctx.max}）` : ''}
          </p>
          <h3 className="evidence-sub">检索命中（{hits.length} 段）</h3>
          <ol className="evidence-list">
            {hits.map((e, i) => <Row key={e.id} item={e} index={i + 1} />)}
          </ol>

          {neighbors.length > 0 && (
            <>
              <h3 className="evidence-sub">同节补充（{neighbors.length} 段，只在生成时使用）</h3>
              <p className="panel__meta">
                按「同 source_id + 同章节」在命中片段左右各补 {ctx?.radius ?? 0} 段，
                补充部分受 {ctx?.max ?? 0} 字符预算约束（真实命中不受限）。
                <strong>补充片段不是检索命中，不计入 Hit@K / MRR。</strong>
              </p>
              <ol className="evidence-list">
                {neighbors.map((e, i) => <Row key={e.id} item={e} index={i + 1} />)}
              </ol>
            </>
          )}
        </>
      )}
    </section>
  );
}
