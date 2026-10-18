import { StateNotice } from './StateNotice';
import { RichBody } from './RichText';
import { Row } from './EvidenceList';
import { refItems, sourceLine } from '../app/derive';
import type { TransportFailure } from '../app/useWorkbench';
import { DATA_SOURCE } from '../services';
import type { MetaResponse, QueryResponse, Run } from '../types';

interface Props {
  run: Run;
  response: QueryResponse | null;
  meta: MetaResponse | null;
  /** 当前控件与结果设置不一致：旧结果仍显示时如实标注（归属行永远读 response.request） */
  stale: boolean;
  /** 传输层失败（本次请求没拿到结果）：消息内先说清楚，再显示上一次的结果 */
  transport: TransportFailure | null;
  /** 正文里 [chunk id] 引用被点击（raw 含方括号） */
  onCite: (raw: string) => void;
  onRetry: () => void;
  onRefocus: () => void;
  onSwitchCorpus: () => void;
}

const MODE_TEXT = { retrieve_only: '仅检索', generate: '检索并生成' } as const;

/**
 * 助手消息（2026-09-29 对话流改版 v1.3，用户拍板 Open WebUI 真对话流）：
 * 回答不再套卡片、不与引用并排；引用对照与证据片段收进消息内部，证据默认折叠。
 * 诚实口径文字全部沿用原 AnswerCard / ReferenceCard / EvidenceList / StateNotice 的原文，
 * 生成层未验证、零结果短路、recorded_top_k 漂移说明等一字未动。
 */
export function ChatMessage({ run, response, meta, stale, transport,
                              onCite, onRetry, onRefocus, onSwitchCorpus }: Props) {
  const busy = run.status === 'retrieving' || run.status === 'generating';
  // 元数据没到之前按构建期数据源判断，避免 api 模式下短暂显示"示例数据"
  const isMock = (meta?.data_source ?? DATA_SOURCE) === 'mock';

  if (busy) {
    return (
      <div className="msg msg--ai">
        <div className="msg__avatar" aria-hidden="true">电</div>
        <div className="msg__body">
          <p className="notice notice--info skeleton" aria-live="polite">
            <span className="skeleton__dot" />
            <span className="skeleton__dot" />
            <span className="skeleton__dot" />
            <span>
              {run.status === 'retrieving' ? '正在检索…' : '正在生成回答…'}{' '}
              {isMock
                ? '本地示例数据，不访问任何真实模型接口。'
                : '数据来自本机 API（127.0.0.1）的实时检索与生成，不读取前端快照。'}
            </span>
          </p>
        </div>
      </div>
    );
  }

  // 没有结果也没有传输失败：外层显示空状态（hello），这里不渲染
  if (!response && !transport) return null;

  const hits = response?.hits ?? [];
  const neighbors = response?.neighbors ?? [];
  const refs = refItems(response);
  const corpus = meta?.corpora.find((c) => c.corpus_id === response?.request.corpus_id);
  const otherCorpus = meta?.corpora.find((c) => c.corpus_id !== response?.request.corpus_id);
  const ctx = response?.context_chars ?? null;

  return (
    <div className="msg msg--ai" id="answer-card">
      <div className="msg__avatar" aria-hidden="true">电</div>
      <div className="msg__body">
        {response && (
          <>
            <div className="msg__who">
              <b>知识库助手</b>
              <span className="chip chip--mode">{MODE_TEXT[response.request.mode]}</span>
              {isMock && <span className="tag tag--mock">示例数据</span>}
            </div>
            <p className="panel__meta msg__meta">
              {response.elapsed_ms.generate
                ? `生成耗时 ${response.elapsed_ms.generate} ms（${isMock ? '已记录的运行值' : '本次实测'}） · `
                : ''}
              <strong>生成层尚未完成正式验证，回答仅供参考</strong>
            </p>
          </>
        )}

        {transport && (
          <p className="notice notice--error" id="transport-notice">
            <strong>
              {transport.kind === 'unreachable'
                ? '连接不上本机 API'
                : transport.kind === 'rejected'
                  ? '本机 API 拒绝了本次请求'
                  : '本机 API 返回了错误'}
            </strong>
            ：{transport.message}
            {' '}
            本次请求没有拿到任何结果
            {response ? '；下面显示的仍是上一次运行的结果，没有被当成这次的。' : '。'}
          </p>
        )}

        {/* 运行结果提示（零命中/无 Key/生成失败/仅检索）：沿用 StateNotice 的原文 */}
        {response && <StateNotice run={run} response={response} />}

        {response?.answer != null ? (
          <div className="msg__content answer-body">
            <RichBody text={response.answer} onCite={onCite} />
          </div>
        ) : response ? (
          <p className="muted msg__noanswer">
            {response.notice === 'zero_hits'
              ? '本次没有生成回答：没有检索命中，生成被短路。'
              : response.notice === 'no_api_key'
                ? '本次没有生成回答：未检测到服务端 API Key。'
                : response.notice === 'generation_failed'
                  ? '本次没有生成回答：模型调用失败，检索结果与证据片段仍完整保留在下方。'
                  : response.notice === 'retrieve_only'
                    ? '本次没有生成回答：当前是仅检索模式。'
                    : '本次没有生成回答。'}
          </p>
        ) : null}

        {/* 缺口卡（零命中）：诚实说明已查什么、为什么短路，并给出下一步动作 */}
        {response?.notice === 'zero_hits' && (
          <div className="gap-card notice notice--warn">
            <strong>未检索到相关资料：不生成回答。</strong>
            <ul>
              <li>
                已检索 <b>{corpus?.name ?? response.request.corpus_id}</b>
                {corpus ? `（${corpus.source_count} 份来源 · ${corpus.chunk_count} 个片段）` : ''}
                ，没有与问题词面相关的片段；
              </li>
              <li>因此没有调用模型（零结果短路），也没有编造回答。</li>
            </ul>
            <div className="gap-card__act">
              <button type="button" className="btn" onClick={onRefocus}>换个问法（更具体的关键词）</button>
              {otherCorpus && (
                <button type="button" className="btn" onClick={onSwitchCorpus}>
                  切到「{otherCorpus.name}」再问
                </button>
              )}
            </div>
          </div>
        )}

        {/* 引用对照：显示完整 chunk id（诚实契约），收进消息内部 */}
        <div className="msg__refs" id="ref-card">
          {refs.length > 0 ? (
            <>
              <p className="panel__meta">
                回答正文里方括号引用共出现 <strong>{response?.ref_occurrences ?? 0}</strong> 次
                （含重复与对不上上下文的写法），重复的只列一次。这里显示<strong>完整 chunk id</strong>，
                与回答正文里的 [id] 一一对应。
              </p>
              <ol className="ref-list">
                {/* 70 号复审 R3：按预览承诺收成紧凑一行——完整 id + 归属 chip + 得分口径；
                    整行是 button（鼠标+键盘），点击与正文 [id] 一样打开引用抽屉，原文详情在抽屉里看 */}
                {refs.map((e) => (
                  <li key={e.id} className="ref-item">
                    <button
                      type="button"
                      className="ref-item__btn"
                      title={`查看引用详情 · ${sourceLine(e)}`}
                      onClick={() => onCite(e.id)}
                    >
                      <code className="ref-item__id">{e.id}</code>
                      <span className={`chip ${e.origin === 'hit' ? 'chip--hit' : 'chip--neighbor'}`}>
                        {e.origin === 'hit' ? '检索命中' : '同节补充'}
                      </span>
                      <span className="ref-item__score">
                        {e.origin === 'hit'
                          ? `BM25 ${e.score?.toFixed(4) ?? '—'} · 排名 ${e.rank ?? '—'}`
                          : `属命中 #${e.rank ?? '—'} · 无 BM25 分（不参与检索排序）`}
                      </span>
                      <span className="ref-item__src">{sourceLine(e)}</span>
                    </button>
                  </li>
                ))}
              </ol>
            </>
          ) : response ? (
            response.answer !== null ? (
              <p className="panel__meta">
                本次回答没有可用引用：回答里没有出现上下文中的任何 chunk id，因此引用检查不通过。
                这只能说明引用解析没对上，不代表回答正确或错误。
              </p>
            ) : response.notice === 'generation_failed' ? (
              <p className="panel__meta">模型调用失败，没有回答可解析引用。</p>
            ) : response.notice === 'zero_hits' ? (
              <p className="panel__meta">没有检索命中，因此没有引用可对照。</p>
            ) : response.notice === 'no_api_key' ? (
              <p className="panel__meta">本次没有调用模型，因此没有引用可对照。</p>
            ) : response.notice === 'retrieve_only' ? (
              <p className="panel__meta">仅检索模式不生成回答，因此没有引用可对照。</p>
            ) : null
          ) : null}
        </div>

        {/* 检索证据：默认折叠的披露框（原文 EvidenceList 的口径与行结构原样收进来） */}
        <details className="disc evidence-card" id="evidence-card">
          <summary id="evidence-title">
            检索证据 · 共 {hits.length} 个检索命中{neighbors.length > 0 ? ` + ${neighbors.length} 个同节补充` : ''}
            {ctx ? ` · 上下文 ${ctx.total} / ${ctx.max} 字符` : ''}（默认折叠，点击展开）
          </summary>
          <div className="disc__body">
            {hits.length > 0 ? (
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
            ) : (
              <p className="muted">{response ? '本次没有检索命中，没有可展示的片段。' : '还没有提问。'}</p>
            )}
          </div>
        </details>

        {/* 结果归属：始终读 response.request（30 号复审契约），改控件不改写它 */}
        {response && (
          <p className="result-scope msg__scope" id="result-scope">
            本次结果使用的设置：知识库 <strong>{corpus?.name ?? response.request.corpus_id}</strong> · 生成模式{' '}
            <strong>{MODE_TEXT[response.request.mode]}</strong> · Top-K <strong>{response.request.top_k}</strong>
            {stale && <span className="stale-flag">（与当前设置不一致）</span>}
          </p>
        )}

        <div className="msg__acts">
          {response?.answer != null && (
            <button
              type="button"
              className="msg__act"
              onClick={() => { navigator.clipboard?.writeText(response.answer ?? '').catch(() => {}); }}
            >
              复制回答
            </button>
          )}
          <button type="button" className="msg__act" onClick={onRetry}>重试</button>
        </div>

        {response?.recorded_from && (
          <p className="panel__meta msg__from">
            数据来源：{response.recorded_from}
            {isMock ? '；本轮未调用任何模型（完整口径见「设置」页使用提示）。' : '。'}
            {response.request.top_k !== response.recorded_top_k
              && ` 注意：这段回答记录于 Top-K=${response.recorded_top_k} 的运行，`
              + `当前 Top-K=${response.request.top_k} 只影响检索证据与引用解析，`
              + '回答正文仍是当时那一次的输出，未重新生成。'}
          </p>
        )}
      </div>
    </div>
  );
}
