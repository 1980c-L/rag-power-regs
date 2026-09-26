import { RichBody } from './RichText';
import { DATA_SOURCE } from '../services';
import type { MetaResponse, QueryResponse, Run } from '../types';

interface Props {
  run: Run;
  response: QueryResponse | null;
  meta: MetaResponse | null;
}

export function AnswerCard({ run, response, meta }: Props) {
  const busy = run.status === 'retrieving' || run.status === 'generating';
  // 元数据没到之前按构建期数据源判断，避免 api 模式下短暂显示"示例数据"
  const isMock = (meta?.data_source ?? DATA_SOURCE) === 'mock';
  const answer = response?.answer ?? null;

  return (
    <section className="card panel answer-card" id="answer-card">
      <header className="panel__head">
        <h2 className="panel__title">回答</h2>
        {isMock && <span className="tag tag--mock">示例数据</span>}
      </header>

      {busy && (
        <div className="skeleton" aria-live="polite">
          <span className="skeleton__dot" />
          <span className="skeleton__dot" />
          <span className="skeleton__dot" />
          <p className="muted">{run.status === 'retrieving' ? '正在检索资料…' : '正在生成回答…'}</p>
        </div>
      )}

      {!busy && answer !== null && (
        <>
          <p className="panel__meta">
            {response?.elapsed_ms.generate
              ? `生成耗时 ${response.elapsed_ms.generate} ms（${isMock ? '已记录的运行值' : '本次实测'}） · `
              : ''}
            <strong>生成层尚未完成正式验证，回答仅供参考</strong>
          </p>
          {response?.recorded_from && (
            <p className="panel__meta muted">
              数据来源：{response.recorded_from}
              {isMock ? '；本轮未调用任何模型（完整口径见右侧「使用提示」）。' : '。'}
              {response.request.top_k !== response.recorded_top_k
                && ` 注意：这段回答记录于 Top-K=${response.recorded_top_k} 的运行，`
                + `当前 Top-K=${response.request.top_k} 只影响检索证据与引用解析，`
                + '回答正文仍是当时那一次的输出，未重新生成。'}
            </p>
          )}
          <div className="answer-body">
            <RichBody text={answer} />
          </div>
        </>
      )}

      {!busy && answer === null && (
        <p className="muted">
          {!response
            ? '还没有提问。输入问题后点击「开始检索」。'
            : response.notice === 'zero_hits'
              ? '本次没有生成回答：没有检索命中，生成被短路。'
              : response.notice === 'no_api_key'
                ? '本次没有生成回答：未检测到服务端 API Key。'
                : response.notice === 'generation_failed'
                  ? '本次没有生成回答：模型调用失败，检索结果与证据片段仍完整保留在下方。'
                  : response.notice === 'retrieve_only'
                    ? '本次没有生成回答：当前是仅检索模式。'
                    : '本次没有生成回答。'}
        </p>
      )}
    </section>
  );
}
