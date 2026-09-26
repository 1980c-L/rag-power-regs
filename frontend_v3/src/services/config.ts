/**
 * 数据源与地址配置 —— **全项目唯一一处**。
 *
 * 第二阶段 A：页面可以通过本机 `api_v3.py`（127.0.0.1）读取真实学习资料与实时检索结果。
 * 切换方式只有构建期环境变量，组件与 hooks 都不认识"服务端地址"这件事：
 *     VITE_DATA_SOURCE=api  VITE_API_BASE=http://127.0.0.1:5280
 * 不设变量时保持第一阶段行为（mock + 本地示例数据），原有验收口径不变。
 */
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) || 'http://127.0.0.1:5280';

export const DATA_SOURCE: 'mock' | 'api' =
  import.meta.env.VITE_DATA_SOURCE === 'api' ? 'api' : 'mock';
