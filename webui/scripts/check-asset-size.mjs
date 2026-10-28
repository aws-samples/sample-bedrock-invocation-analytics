// Fails the build if any file could exceed the 1 MB ALB → Lambda response limit (gzip + base64, with headroom).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'

const LIMIT = 1_048_576
const HEADROOM = 32 * 1024 // JSON envelope and headers
const files = (dir) => readdirSync(dir).flatMap((f) => (statSync(join(dir, f)).isDirectory() ? files(join(dir, f)) : [join(dir, f)]))

const tooBig = files('dist').map((file) => {
  const raw = readFileSync(file)
  const smallest = Math.min(raw.length, gzipSync(raw, { level: 6 }).length)
  return { file, encoded: Math.ceil(smallest / 3) * 4 }
}).filter(({ encoded }) => encoded > LIMIT - HEADROOM)

for (const { file, encoded } of tooBig) console.error(`${file}: ${encoded} bytes after gzip+base64 exceeds the ALB Lambda limit`)
process.exit(tooBig.length ? 1 : 0)
