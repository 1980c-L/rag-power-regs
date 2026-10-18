import { useCallback, useState } from 'react';
import { useWorkbench } from './app/useWorkbench';
import { BatchPanel } from './components/BatchPanel';
import { ChatMessage } from './components/ChatMessage';
import { QuestionCard } from './components/QuestionCard';
import { SettingsPanel } from './components/SettingsPanel';
import { TopBar } from './components/TopBar';
import { UserLibraryPanel } from './components/UserLibraryPanel';
import { DATA_SOURCE } from './services';
import type { EvidenceItem, GenerateMode, MetaResponse } from './types';

/** 2026-09-28 重设计：左侧文字导航 + 四视图（对话 / 资料库 / 批量任务 / 设置）。
    视觉规范：Open WebUI 系（D 系 mockup，用户选定）；对话列居中，其余页左对齐。
    2026-09-29 对话流改版（v1.3，用户拍板）：对话页改为 Open WebUI 式真对话流——
    步骤条/双卡并排/常驻证据表 → 消息流（用户气泡在右、回答裸排在左），证据默认折叠，
    参数收进输入条；数据与结果归属逻辑（result-scope / drift / transport）原样保留。 */
type View = 'chat' | 'lib' | 'batch' | 'set';

const VIEW_TITLES: Record<View, string> = {
  chat: '对话',
  lib: '资料库',
  batch: '批量任务',
  set: '设置',
};

const NAV_ICONS: Record<View, JSX.Element> = {
  chat: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.3c-1.2 0-2.4-.2-3.4-.7L4 20l1-4.1a8 8 0 0 1-1.5-4.4A8.4 8.4 0 0 1 12 3.2a8.4 8.4 0 0 1 9 8.3z" /></svg>
  ),
  lib: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" /><path d="M4 19a2 2 0 0 0 2 2h13" /><path d="M9 7h6" /></svg>
  ),
  batch: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3 3 8l9 5 9-5-9-5z" /><path d="m3 13 9 5 9-5" /></svg>
  ),
  set: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9l2.1 2.1m10 10 2.1 2.1m0-14.2-2.1 2.1m-10 10-2.1 2.1" /></svg>
  ),
};

const MODE_TEXT: Record<GenerateMode, string> = {
  retrieve_only: '仅检索',
  generate: '检索并生成',
};

function corpusName(meta: MetaResponse | null, id: string): string {
  if (!meta) return id;
  return meta.corpora.find((c) => c.corpus_id === id)?.name ?? id;
}

export function App() {
  const wb = useWorkbench();
  const [view, setView] = useState<View>('chat');
  const [citeItem, setCiteItem] = useState<EvidenceItem | null>(null);
  const currentCorpus = corpusName(wb.meta, wb.settings.corpus_id);
  const busy = wb.run.status === 'retrieving' || wb.run.status === 'generating';

  // 引用抽屉：正文 [chunk id] / 引用对照点击 → 右侧详情（v1.3 对话流）
  const openCite = useCallback((raw: string) => {
    const res = wb.response;
    if (!res) return;
    const id = raw.replace(/^\[|\]$/g, '').replace(/^来源：/, '');
    const item = [...res.hits, ...res.neighbors].find((e) => e.id === id);
    if (item) setCiteItem(item);
  }, [wb.response]);
  const retryCurrent = useCallback(() => {
    // 70 号复审 R2：本次请求失败（传输失败或结果错误）时，重试的应是失败的那次提问（run.question）；
    // 只有对正常完成的结果做"重跑"，才沿用该结果的问题（response.request.question）。
    const failed = busy || wb.run.status === 'error';
    wb.askQuestion(failed ? wb.run.question : (wb.response?.request.question ?? wb.run.question));
  }, [wb, busy]);
  const refocusAsk = useCallback(() => {
    document.getElementById('question')?.focus();
  }, []);
  const switchCorpus = useCallback(() => {
    const current = wb.response?.request.corpus_id ?? wb.settings.corpus_id;
    const other = (wb.meta?.corpora ?? []).find((c) => c.corpus_id !== current);
    if (other) wb.updateSettings({ corpus_id: other.corpus_id });
  }, [wb]);
  const suggest = useCallback((q: string) => {
    wb.setQuestion(q);
    document.getElementById('question')?.focus();
  }, [wb]);

  // 结果的真实归属：始终读 response.request，绝不读当前控件值（归属行由 ChatMessage 渲染）
  const driftText = wb.drift.any
    ? [
      wb.drift.corpus ? `知识库 → ${currentCorpus}` : '',
      wb.drift.mode ? `生成模式 → ${MODE_TEXT[wb.settings.mode]}` : '',
      wb.drift.top_k ? `Top-K → ${wb.settings.top_k}` : '',
    ].filter(Boolean).join('、')
    : '';

  return (
    <div className="page" data-datasource={DATA_SOURCE} data-forced-state={wb.forcedState ?? ''}>
      <aside className="sidebar">
        <div className="sb-brand">
          <span className="sb-brand__mark" aria-hidden="true">R</span>
          <div>
            <h1 className="sb-brand__title">RAG 问答工作台</h1>
            <p className="sb-brand__sub">本机知识库 · 检索 BM25</p>
          </div>
        </div>
        <nav className="sb-nav" aria-label="页面导航">
          {(['chat', 'lib', 'batch', 'set'] as View[]).map((v) => (
            <button
              key={v}
              type="button"
              className={`sb-navbtn ${view === v ? 'is-active' : ''}`}
              data-view={v}
              onClick={() => setView(v)}
            >
              {NAV_ICONS[v]}
              <span>{VIEW_TITLES[v]}</span>
            </button>
          ))}
        </nav>
        <div className="sb-foot">
          <span>知识库：{currentCorpus}</span>
          <span>生成层尚未完成正式验证</span>
        </div>
      </aside>

      <div className="main">
        <TopBar
          title={VIEW_TITLES[view]}
          meta={wb.meta}
          corpusName={currentCorpus}
          metaError={wb.metaError}
        />
        <div className="view">

          {/* 2026-09-28 R1 窄修（69 号复审）：四页常驻挂载、仅切换可见性——
              离开页面不再卸载组件，批量草稿/参数与资料库草稿在会话内跨页保留。
              数据运行状态本就存于 useWorkbench（App 级），不受挂载影响。 */}
          <div id="pane-chat" className={`viewpane ${view === 'chat' ? '' : 'is-hidden'}`}>
            {/* 2026-09-29 对话流改版（v1.3）：步骤条/双卡并排/常驻证据表 → 单轮问答消息流，
                用户气泡在右、回答裸排在左；引用对照与证据片段收进消息（证据默认折叠）；
                漂移提示挪到输入条上方；参数收进输入条。数据语义（单条结果 + 归属契约）不变。 */}
            <main className="main-col">
              <div className="chat-scroll" id="chat-scroll">
                {!busy && !wb.response && !wb.transport && (
                  <div className="hello" id="hello">
                    <p className="hello__title">RAG 问答工作台</p>
                    <p className="hello__sub">
                      向知识库提问，回答附完整 chunk id 引用，可点击回溯出处；资料里没有的内容会明确说明，不会编造。
                      还没有提问。点下面的问题可快速填入，也可以直接输入。
                    </p>
                    <div className="sugs">
                      <button type="button" onClick={() => suggest('RAG 评估三元组包含哪三个维度？')}>
                        RAG 评估三元组包含哪三个维度？
                      </button>
                      <button type="button" onClick={() => suggest('BM25 得分代表什么？是不是分越高回答越正确？')}>
                        BM25 得分代表什么？是不是分越高回答越正确？
                      </button>
                      <button type="button" onClick={() => suggest('同节补充的片段和检索命中有什么区别？')}>
                        同节补充的片段和检索命中有什么区别？
                      </button>
                    </div>
                  </div>
                )}
                {(busy || wb.response || wb.transport) && (
                  <div className="turn">
                    <div className="msg msg--user">
                      <div className="msg__bubble">
                        {busy ? wb.run.question : (wb.response?.request.question ?? wb.run.question)}
                      </div>
                      <div className="msg__avatar" aria-hidden="true">P</div>
                    </div>
                    <ChatMessage
                      run={wb.run}
                      response={wb.response}
                      meta={wb.meta}
                      stale={wb.drift.any}
                      transport={wb.transport}
                      onCite={openCite}
                      onRetry={retryCurrent}
                      onRefocus={refocusAsk}
                      onSwitchCorpus={switchCorpus}
                    />
                  </div>
                )}
              </div>
              {driftText && (
                <p className="notice notice--warn" id="stale-notice">
                  <strong>设置已变更</strong>（{driftText}），上面显示的仍是<strong>上一次运行</strong>的结果；
                  请重新点击「开始检索」。结果归属以「本次结果使用的设置」为准。
                </p>
              )}
              <QuestionCard
                question={wb.question}
                onQuestion={wb.setQuestion}
                onAsk={wb.ask}
                onSettings={wb.updateSettings}
                settings={wb.settings}
                meta={wb.meta}
                busy={busy}
              />
            </main>
            {citeItem && (
              <>
                <div className="drawer-backdrop" onClick={() => setCiteItem(null)} />
                <aside className="cite-drawer" role="dialog" aria-label="引用详情">
                  <div className="cite-drawer__head">
                    <b>引用详情</b>
                    <button type="button" className="cite-drawer__x" aria-label="关闭" onClick={() => setCiteItem(null)}>✕</button>
                  </div>
                  <div className="cite-drawer__body">
                    <p className="kv"><span>chunk id</span><code>{citeItem.id}</code></p>
                    <p className="kv"><span>来源</span><span>{citeItem.source_title || citeItem.source_id}</span></p>
                    {citeItem.section && <p className="kv"><span>小节</span><span>{citeItem.section}</span></p>}
                    <p className="kv">
                      <span>归属</span>
                      <span>
                        {citeItem.origin === 'hit'
                          ? `检索命中 · 排名 ${citeItem.rank ?? '—'} · BM25 ${citeItem.score?.toFixed(4) ?? '—'}`
                          : `同节补充 · 属命中 #${citeItem.rank ?? '—'} · 无 BM25 分（不参与检索排序）`}
                      </span>
                    </p>
                    {citeItem.source_url && (
                      <p className="kv"><span>原文</span><a href={citeItem.source_url} target="_blank" rel="noreferrer">查看原文</a></p>
                    )}
                    <blockquote className="cite-drawer__quote">{citeItem.text}</blockquote>
                    <p className="muted small">
                      BM25 得分只表示该段与问题在关键词上的相关程度排序，不是置信度、不是正确率；检索层没有相关性阈值。
                    </p>
                  </div>
                </aside>
              </>
            )}
          </div>

          <div id="pane-lib" className={`viewpane ${view === 'lib' ? '' : 'is-hidden'}`}>
            <section className="panel-view" aria-label="资料库">
              <div className="pagehead">
                <h2 className="pagehead__title">资料库</h2>
                <p className="pagehead__sub">个人资料的管理入口；检索与内置库走同一条 BM25 链路，不参与评测</p>
              </div>
              {DATA_SOURCE === 'api' ? (
                <UserLibraryPanel meta={wb.meta?.user_library ?? null} locked={busy} onChanged={wb.refreshMeta} />
              ) : (
                <p className="notice notice--info">
                  <strong>资料管理需要本机 API。</strong>
                  当前是本地示例数据模式（第一阶段），没有服务端可存资料，因此这里不做任何假装可用的入口。
                </p>
              )}
            </section>
          </div>

          <div id="pane-batch" className={`viewpane ${view === 'batch' ? '' : 'is-hidden'}`}>
            <section className="panel-view" aria-label="批量任务">
              <div className="pagehead">
                <h2 className="pagehead__title">批量任务</h2>
                <p className="pagehead__sub">执行语义全在服务端：1–20 条硬上限 · 串行 · 每题最多 1 次请求 · 零自动重试</p>
              </div>
              {/* 批量提问：独立入口（47 号方案阶段 A）。它读同一份 meta，但自己一套控件，
                  不会改写单题结果，也不参与单题结果归属（result-scope）的判定。 */}
              <BatchPanel meta={wb.meta} locked={busy} />
            </section>
          </div>

          <div id="pane-set" className={`viewpane ${view === 'set' ? '' : 'is-hidden'}`}>
            <section className="panel-view" aria-label="设置">
              <div className="pagehead">
                <h2 className="pagehead__title">设置</h2>
                <p className="pagehead__sub">与对话页输入条是同一套设置；改动后，上一次结果会标注「与当前设置不一致」</p>
              </div>
              <SettingsPanel
                meta={wb.meta}
                settings={wb.settings}
                onChange={wb.updateSettings}
                locked={busy}
              />
            </section>
          </div>

        </div>
      </div>
    </div>
  );
}
