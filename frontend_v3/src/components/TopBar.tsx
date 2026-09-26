import { DATA_SOURCE } from '../services';
import type { MetaResponse } from '../types';

interface Props {
  meta: MetaResponse | null;
  corpusName: string;
  /** api 模式下元数据没取到：如实说"不可达"，不退回成"示例数据" */
  metaError?: boolean;
}

/**
 * 顶栏：品牌 + 数据来源标签 + 一句话说明。
 * 数据来源只有两种，必须如实标注：
 *   mock → 第一阶段：本地示例数据；
 *   api  → 已接入本机 API（真实学习库）；连不上就写"不可达"，绝不假装已接入。
 */
export function TopBar({ meta, corpusName, metaError = false }: Props) {
  const isMock = DATA_SOURCE === 'mock';
  const tag = isMock
    ? { cls: 'tag tag--mock', text: '第一阶段：本地示例数据' }
    : metaError
      ? { cls: 'tag tag--warn', text: '本机 API 不可达（未取到元数据）' }
      : meta
        ? { cls: 'tag tag--api', text: '已接入本机 API（真实学习库）' }
        : { cls: 'tag', text: '正在连接本机 API…' };

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <span className="topbar__logo" aria-hidden="true">🔍</span>
        <h1 className="topbar__title">RAG 问答工作台</h1>
        <span className={tag.cls}>{tag.text}</span>
      </div>
      <p className="topbar__sub">
        {corpusName} · 检索 BM25（关键词）· 生成层尚未完成正式验证
      </p>
    </header>
  );
}
