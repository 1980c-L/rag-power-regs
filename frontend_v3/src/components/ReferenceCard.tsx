import { clip, refItems, sourceLine } from '../app/derive';
import type { QueryResponse, Run } from '../types';

interface Props {
  run: Run;
  response: QueryResponse | null;
}

/**
 * 引用对照：显示**完整 chunk id**（不做 [1][2][3] 简写、不画跳转链接），
 * 并逐条标注属于「检索命中」还是「同节补充」。无有效引用时如实说明，不假装完成对照。
 */
export function ReferenceCard({ run, response }: Props) {
  const busy = run.status === 'retrieving' || run.status === 'generating';
  const items = refItems(response);
  const answer = response?.answer ?? null;

  return (
    <section className="card panel ref-card" id="ref-card">
      <header className="panel__head">
        <h2 className="panel__title">引用对照</h2>
        {items.length > 0 && <span className="count">{items.length} 处不同来源</span>}
      </header>

      {busy && <p className="muted">等待生成结果…</p>}

      {!busy && items.length > 0 && (
        <>
          <p className="panel__meta">
            回答正文里方括号引用共出现 <strong>{response?.ref_occurrences ?? 0}</strong> 次
            （含重复与对不上上下文的写法），重复的只列一次。这里显示<strong>完整 chunk id</strong>，
            与回答正文里的 [id] 一一对应。
          </p>
          <ol className="ref-list">
            {items.map((e) => (
              <li key={e.id} className="ref-item">
                <div className="ref-item__head">
                  <code className="ref-item__id">{e.id}</code>
                  <span className={`chip ${e.origin === 'hit' ? 'chip--hit' : 'chip--neighbor'}`}>
                    {e.origin === 'hit' ? '检索命中' : '同节补充'}
                  </span>
                </div>
                <p className="ref-item__src">{sourceLine(e)}</p>
                <p className="ref-item__clip">{clip(e.text)}</p>
              </li>
            ))}
          </ol>
        </>
      )}

      {!busy && items.length === 0 && (
        <p className="muted">
          {answer !== null
            ? <>本次回答没有可用引用：回答里没有出现上下文中的任何 chunk id，因此第 4 步不点亮。这只能说明引用解析没对上，不代表回答正确或错误。</>
            : response?.notice === 'generation_failed'
              ? '模型调用失败，没有回答可解析引用。'
              : response?.notice === 'zero_hits'
                ? '没有检索命中，因此没有引用可对照。'
                : response?.notice === 'no_api_key'
                  ? '本次没有调用模型，因此没有引用可对照。'
                  : response?.notice === 'retrieve_only'
                    ? '仅检索模式不生成回答，因此没有引用可对照。'
                    : '还没有提问。'}
        </p>
      )}
    </section>
  );
}
