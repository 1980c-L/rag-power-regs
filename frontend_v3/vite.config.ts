import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 端口固定：浏览器检查脚本要按固定地址访问，被占用时应直接报错而不是换端口
export default defineConfig({
  plugins: [react()],
  server: { port: 5273, strictPort: true, host: '127.0.0.1' },
  preview: { port: 5273, strictPort: true, host: '127.0.0.1' },
});
