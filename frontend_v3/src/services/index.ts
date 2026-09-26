/**
 * 数据源选择。**只在这里选择 mock / api**，组件、hooks 都只认 `service`。
 *
 * 第一阶段（默认）：mockService —— 本地示例数据，数值来自真实检索快照；
 * 第二阶段 A：apiService —— 通过本机 127.0.0.1 的 `api_v3.py` 读真实学习资料并实时检索。
 *
 * 切换靠构建期环境变量（见 services/config.ts）：
 *     VITE_DATA_SOURCE=api  VITE_API_BASE=http://127.0.0.1:5280
 * 未设置时行为与第一阶段完全一致，原有 39 项验收不回退。
 */
import { mockService } from './mockService';
import { apiService } from './apiService';
import { DATA_SOURCE } from './config';
import type { DataService } from '../types';

export { DATA_SOURCE, API_BASE } from './config';

export const service: DataService = DATA_SOURCE === 'api' ? apiService : mockService;
