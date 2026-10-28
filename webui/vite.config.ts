import { readFileSync } from 'node:fs'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const version = /^version\s*=\s*"([^"]+)"/m.exec(readFileSync('../pyproject.toml', 'utf8'))?.[1] ?? ''
// `npm run dev` proxies API calls to a deployed stack, e.g. WEBUI_URL=https://dxxxx.cloudfront.net
const target = process.env.WEBUI_URL

export default defineConfig({
  plugins: [react()],
  define: { 'import.meta.env.VITE_APP_VERSION': JSON.stringify(version) },
  server: target ? { proxy: { '/api': { target, changeOrigin: true }, '/config.json': { target, changeOrigin: true } } } : undefined,
  build: {
    sourcemap: false,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'cloudscape', test: /node_modules[\\/]@cloudscape-design/ },
            { name: 'echarts', test: /node_modules[\\/](echarts|zrender)/ },
            { name: 'amplify', test: /node_modules[\\/](@aws-amplify|aws-amplify|@aws-crypto|@smithy|@aws-sdk)/ },
            { name: 'react', test: /node_modules[\\/](react|react-dom|react-router|scheduler|@tanstack)[\\/]/ },
          ],
        },
      },
    },
  },
})
