import { RichText } from './RichText';
import { UserLibraryPanel } from './UserLibraryPanel';
import { DATA_SOURCE } from '../services';
import type { CorpusId, GenerateMode, MetaResponse, Settings } from '../types';

interface Props {
  meta: MetaResponse | null;
  settings: Settings;
  onChange: (patch: Partial<Settings>) => void;
  locked: boolean;
  /** 资料库增删后刷新 meta，让「我的资料库」选项即时出现/消失 */
  onMetaRefresh?: () => void;
}

/** 右侧设置栏：知识库 / 生成模式 / Top-K 的唯一入口 + 资料与参数 + 使用提示 */
export function SettingsPanel({ meta, settings, onChange, locked, onMetaRefresh }: Props) {
  const corpora = meta?.corpora ?? [];
  const corpus = corpora.find((c) => c.corpus_id === settings.corpus_id);
  // 注意用 ?? null 兜底：meta 还没到（api 模式接口不可达时永远不到）时字段是 undefined，
  // 若直接判断 === null 会三个分支都不命中，Key 状态框整块空白
  const key = meta?.api_key_configured ?? null;

  return (
    <aside className="aside">
      <section className="card aside-card">
        <h2 className="aside-card__title">设置</h2>

        <fieldset className="field" disabled={locked}>
          <legend className="field__label">知识库</legend>
          <div className="options">
            {corpora.map((c) => (
              <label key={c.corpus_id} className="option">
                <input
                  type="radio"
                  name="corpus"
                  value={c.corpus_id}
                  checked={settings.corpus_id === c.corpus_id}
                  onChange={() => onChange({ corpus_id: c.corpus_id as CorpusId })}
                />
                <span>{c.name}</span>
              </label>
            ))}
            {/* 我的资料库：只在库里有资料且来自服务端 meta 时出现（排在最后，不打乱既有验收顺序） */}
            {meta?.user_library?.available && (
              <label className="option" id="option-user-library">
                <input
                  type="radio"
                  name="corpus"
                  value="user_library"
                  checked={settings.corpus_id === 'user_library'}
                  onChange={() => onChange({ corpus_id: 'user_library' })}
                />
                <span>{meta.user_library.name}</span>
              </label>
            )}
          </div>
        </fieldset>
        {corpus?.note && <p className="muted small">{corpus.note}</p>}
        {settings.corpus_id === 'user_library' && meta?.user_library && (
          <p className="muted small">{meta.user_library.note}</p>
        )}

        <fieldset className="field" disabled={locked}>
          <legend className="field__label">生成模式</legend>
          <div className="options">
            {([['retrieve_only', '仅检索'], ['generate', '检索并生成']] as [GenerateMode, string][])
              .map(([value, label]) => (
                <label key={value} className="option">
                  <input
                    type="radio"
                    name="mode"
                    value={value}
                    checked={settings.mode === value}
                    onChange={() => onChange({ mode: value })}
                  />
                  <span>{label}</span>
                </label>
              ))}
          </div>
        </fieldset>

        <label className="field">
          <span className="field__label">Top-K（检索返回的段数）</span>
          <select
            className="select"
            value={settings.top_k}
            disabled={locked}
            onChange={(e) => onChange({ top_k: Number(e.target.value) })}
          >
            {(meta?.top_k_choices ?? [1, 2, 3, 4, 5]).map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>

        <div className={`keybox ${key === true ? 'keybox--ok' : key === false ? 'keybox--warn' : ''}`}>
          {key === true && <>API Key：服务端已检测到（来自环境变量）</>}
          {key === false && <>API Key：未检测到</>}
          {key === null && (DATA_SOURCE === 'mock'
            ? <>API Key：未接入服务端，状态未知</>
            : <>API Key：本机 API 不可达，状态未知</>)}
        </div>
        <p className="muted small">
          界面不显示 key 内容，也不提供前端开关。
          {key === null && (DATA_SOURCE === 'mock'
            ? ' 第一阶段只有本地示例数据，没有服务端可检测，因此不假装“已检测到”。'
            : ' 本机 API 当前不可达，取不到 Key 状态，因此如实写“未知”。')}
        </p>
      </section>

      <section className="card aside-card">
        <h2 className="aside-card__title">资料与参数</h2>
        {settings.corpus_id === 'user_library' && meta?.user_library ? (
          <>
            <p className="muted small">
              来源 <strong>{meta.user_library.source_count}</strong> 份 · chunk{' '}
              <strong>{meta.user_library.chunk_count}</strong> 个 · {meta.user_library.total_chars} 字符
            </p>
            <p className="muted small">切分上限 400 字符（无 overlap），与内置库同一条切分链路</p>
            <p className="muted small">
              本库不参与评测：<strong>没有 Hit@K / MRR</strong>，也不继承内置库指标；
              指纹随资料增删变化。
            </p>
          </>
        ) : corpus ? (
          <>
            <p className="muted small">
              来源 <strong>{corpus.source_count}</strong> 份 · chunk <strong>{corpus.chunk_count}</strong> 个 ·{' '}
              {corpus.total_chars} 字符
            </p>
            <p className="muted small">切分上限 {corpus.params.chunk_max_chars} 字符（无 overlap）</p>
            <details className="more">
              <summary>更多信息</summary>
              <ul className="more__list">
                <li>版本：{corpus.version}</li>
                <li>分词器：{corpus.tokenizer}；BM25 k1={corpus.params.bm25_k1} b={corpus.params.bm25_b}；Top-K 默认 {corpus.params.top_k_default}</li>
                <li>切分链路：{corpus.chunking_note}</li>
                <li>检索只有 <strong>BM25 关键词检索</strong>：没有向量检索、混合检索、reranker、多轮对话。</li>
                <li>BM25 得分是排序分，不是置信度；检索层没有相关性阈值。</li>
              </ul>
            </details>
          </>
        ) : (
          <p className="muted small">加载中…</p>
        )}
      </section>

      {/* 我的资料库管理入口（仅 api 模式；mock 模式没有服务端，不假装能用） */}
      {DATA_SOURCE === 'api' && (
        <UserLibraryPanel meta={meta?.user_library ?? null} locked={locked} onChanged={onMetaRefresh} />
      )}

      <section className="card aside-card">
        <h2 className="aside-card__title">使用提示</h2>
        <ul className="tips">
          <li><strong>命中 ≠ 答对</strong>：Hit@K 只检查规定的证据片段有没有进前 K 名，不评判回答质量。</li>
          <li><strong>补充 ≠ 命中</strong>：同节补充是生成时才补入的上下文，不计入任何检索指标。</li>
          <li><RichText text={meta?.unverified_note ?? '生成层尚未完成正式验证，回答仅供参考。'} /></li>
          <li>「尚未验证（NOT VALIDATED）」不等于「验证失败」，两者分开表达。</li>
          {corpus?.is_sample_only
            ? <li>本库是<strong>示例资料，不是正式规程</strong>，不能用于现场作业或安全决策。</li>
            : settings.corpus_id === 'user_library'
              ? <li>个人资料库的检索与内置库同一条 BM25 链路；命中同样<strong>不等于答对</strong>。</li>
              : <li>学习库资料来自 Datawhale《All-in-RAG》，许可 CC BY-NC-SA 4.0（署名 · 非商业 · 相同方式共享）。</li>}
        </ul>
      </section>
    </aside>
  );
}
