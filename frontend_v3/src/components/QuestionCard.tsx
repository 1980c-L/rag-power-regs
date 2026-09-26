import type { MetaResponse, Settings } from '../types';

interface Props {
  question: string;
  onQuestion: (q: string) => void;
  onAsk: () => void;
  settings: Settings;
  meta: MetaResponse | null;
  busy: boolean;
}

/** 提问卡片：只有输入框 + 提交按钮；知识库/模式/Top-K 的唯一入口在右侧设置栏 */
export function QuestionCard({ question, onQuestion, onAsk, settings, meta, busy }: Props) {
  const corpus = meta?.corpora.find((c) => c.corpus_id === settings.corpus_id);
  const modeText = settings.mode === 'generate' ? '检索并生成' : '仅检索';
  return (
    <section className="card ask-card">
      <div className="ask-card__row">
        <input
          id="question"
          className="ask-card__input"
          type="text"
          value={question}
          placeholder="例如：RAG 评估三元组包含哪三个维度？"
          aria-label="输入你的问题"
          onChange={(e) => onQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') onAsk(); }}
        />
        <button type="button" className="btn btn--primary" onClick={onAsk} disabled={busy}>
          {busy ? '检索中…' : '开始检索'}
        </button>
      </div>
      <p className="ask-card__summary">
        当前设置：知识库 <strong>{corpus?.name ?? '—'}</strong> · 生成模式{' '}
        <strong>{modeText}</strong> · Top-K <strong>{settings.top_k}</strong>
        （只在右侧「设置」里改，主区不重复放控件）
      </p>
      {corpus?.is_sample_only && (
        <p className="inline-warn">{corpus.warning}</p>
      )}
      {settings.corpus_id === 'rag_learning' && corpus?.license_note && (
        <p className="ask-card__license">资料来源与许可：{corpus.version} · {corpus.license_note}</p>
      )}
    </section>
  );
}
