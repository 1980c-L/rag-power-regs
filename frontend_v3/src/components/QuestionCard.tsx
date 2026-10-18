import type { CorpusId, MetaResponse, Settings } from '../types';

interface Props {
  question: string;
  onQuestion: (q: string) => void;
  onAsk: () => void;
  /** v1.3 对话流：参数收进输入条（与「设置」页同一套 settings，同一 updateSettings） */
  onSettings: (patch: Partial<Settings>) => void;
  settings: Settings;
  meta: MetaResponse | null;
  busy: boolean;
}

/** 底部输入条（2026-09-29 对话流改版）：问题输入 + 参数行（生成模式 / 知识库 / Top-K）。
    参数与「设置」页读写同一份 settings；诚实口径（示例库警告、许可说明）原样保留。 */
export function QuestionCard({ question, onQuestion, onAsk, onSettings, settings, meta, busy }: Props) {
  const corpus = meta?.corpora.find((c) => c.corpus_id === settings.corpus_id);
  const corpora = meta?.corpora ?? [];
  return (
    <section className="card ask-card" aria-label="提问">
      <div className="ask-card__row">
        <textarea
          id="question"
          className="ask-card__input"
          rows={1}
          value={question}
          placeholder="向知识库提问…（Enter 发送 / Shift+Enter 换行）"
          aria-label="输入你的问题"
          onChange={(e) => onQuestion(e.target.value)}
          onKeyDown={(e) => {
            // 70 号复审 R1：输入法组合中的 Enter（isComposing；keyCode 229 兜底 Safari）是"选字确认"，
            // 不是发送；busy 时不重复发起（查询入口 useWorkbench.runQuery 另有单飞防护）。
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
            if (e.key === 'Enter' && !e.shiftKey && !busy) {
              e.preventDefault();
              onAsk();
            }
          }}
        />
        <button type="button" className="btn btn--primary" onClick={onAsk} disabled={busy}>
          {busy ? '检索中…' : '开始检索'}
        </button>
      </div>
      <div className="ask-card__params">
        <div className="seg" role="group" aria-label="生成模式">
          <button
            type="button"
            className={settings.mode === 'retrieve_only' ? 'seg__btn is-on' : 'seg__btn'}
            disabled={busy}
            onClick={() => onSettings({ mode: 'retrieve_only' })}
          >
            仅检索
          </button>
          <button
            type="button"
            className={settings.mode === 'generate' ? 'seg__btn is-on' : 'seg__btn'}
            disabled={busy}
            onClick={() => onSettings({ mode: 'generate' })}
          >
            检索并生成
          </button>
        </div>
        <select
          className="ask-card__select"
          aria-label="知识库"
          value={settings.corpus_id}
          disabled={busy || !meta}
          onChange={(e) => onSettings({ corpus_id: e.target.value as CorpusId })}
        >
          {corpora.map((c) => (
            <option key={c.corpus_id} value={c.corpus_id}>{c.name}</option>
          ))}
          {/* 我的资料库：与「设置」页同源，只在服务端 meta 里可用时出现 */}
          {meta?.user_library?.available && (
            <option value="user_library">{meta.user_library.name}</option>
          )}
        </select>
        <select
          className="ask-card__select"
          aria-label="Top-K（检索返回的段数）"
          value={settings.top_k}
          disabled={busy || !meta}
          onChange={(e) => onSettings({ top_k: Number(e.target.value) })}
        >
          {(meta?.top_k_choices ?? [1, 2, 3, 4, 5]).map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </div>
      {corpus?.is_sample_only && <p className="inline-warn">{corpus.warning}</p>}
      {settings.corpus_id === 'user_library' && meta?.user_library && (
        <p className="ask-card__summary">{meta.user_library.note}</p>
      )}
      {settings.corpus_id === 'rag_learning' && corpus?.license_note && (
        <p className="ask-card__license">资料来源与许可：{corpus.version} · {corpus.license_note}</p>
      )}
    </section>
  );
}
