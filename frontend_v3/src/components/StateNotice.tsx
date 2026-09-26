import { DATA_SOURCE } from '../services';
import type { QueryResponse, Run } from '../types';

interface Props {
  run: Run;
  response: QueryResponse | null;
}

/** 状态提示：if/else 链，同一时刻最多一条（25 号第 2 条） */
export function StateNotice({ run, response }: Props) {
  const isMock = DATA_SOURCE === 'mock';
  if (run.status === 'retrieving' || run.status === 'generating') {
    return (
      <p className="notice notice--info">
        <strong>{run.status === 'retrieving' ? '正在检索…' : '正在生成回答…'}</strong>
        {' '}
        {isMock
          ? '本地示例数据，不访问任何真实模型接口。'
          : '数据来自本机 API（127.0.0.1）的实时检索与生成，不读取前端快照。'}
      </p>
    );
  }
  if (!response) return null;

  if (response.notice === 'zero_hits') {
    return (
      <p className="notice notice--warn">
        <strong>未检索到相关资料：不生成回答。</strong>
        当前知识库里没有与问题词面相关的片段，因此没有调用模型（零结果短路）。
      </p>
    );
  }
  if (response.notice === 'no_api_key') {
    return (
      <p className="notice notice--info">
        <strong>无 API Key：仅展示检索结果。</strong>
        未检测到服务端 API Key，因此不调用模型；界面不提供 key 输入框，也不会回显 key。
      </p>
    );
  }
  if (response.notice === 'generation_failed') {
    return (
      <p className="notice notice--error">
        <strong>模型调用失败：已保留检索到的资料。</strong>
        {response.notice_detail || '错误信息见下方回答区。'}
      </p>
    );
  }
  if (response.notice === 'retrieve_only') {
    return (
      <p className="notice notice--info">
        <strong>仅检索模式：不调用模型。</strong>
        下面只有 BM25 检索结果与证据片段，没有回答与引用对照。
      </p>
    );
  }
  return null;
}
