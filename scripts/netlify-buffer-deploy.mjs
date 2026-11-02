/** Netlify CLI 27.16.1, unchanged deploy pipeline with a scoped API instance hook.
 * Usage: node scripts/netlify-buffer-deploy.mjs deploy [official deploy flags]
 * The supervisor must still validate the public package before invoking this.
 * No dependency files, ESM loaders, source rewrites, auth or endpoint overrides.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';

export const PINNED_CLI_VERSION='27.16.1';
// CLI keeps its default five concurrent uploads: at most 160 MiB payload
// buffers, with a 32 MiB per-file cap above the largest 16.2 MiB public PDF.
export const MAX_UPLOAD_BYTES=32*1024*1024;
function bounded(bytes) {if(bytes>MAX_UPLOAD_BYTES)throw Error('netlify_upload_exceeds_32mb');}
export const DEFAULT_CLI_ROOT=fileURLToPath(new URL('../runtime/service/netlify-cli/node_modules/netlify-cli/',import.meta.url));
const wrapped=new WeakSet();

export async function uploadBuffer(body) {
  const value=typeof body==='function'?await body():body;
  if(Buffer.isBuffer(value)){bounded(value.length);return value;}
  if(typeof value==='string'){bounded(Buffer.byteLength(value));return Buffer.from(value);}
  if(value instanceof Uint8Array||value instanceof ArrayBuffer){bounded(value.byteLength);return Buffer.from(value);}
  if(value&&typeof value[Symbol.asyncIterator]==='function') {
    const chunks=[];let length=0;
    // Consume and close the file stream before sending; retries get a replayable
    // Buffer factory, so node-fetch supplies an exact Content-Length each time.
    for await(const chunk of value) {
      if(typeof chunk!=='string'&&!(chunk instanceof Uint8Array))throw Error('unsupported_netlify_upload_chunk');
      length+=typeof chunk==='string'?Buffer.byteLength(chunk):chunk.byteLength;bounded(length);
      chunks.push(Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }
  throw Error('unsupported_netlify_upload_body');
}

export function bufferFileUploads(api) {
  if(!api||typeof api.uploadDeployFile!=='function')throw Error('netlify_upload_api_unavailable');
  if(wrapped.has(api))return api;
  const original=api.uploadDeployFile;
  api.uploadDeployFile=async(params,options)=>{
    const bytes=await uploadBuffer(params.body);
    return original.call(api,{...params,body:()=>bytes},options);
  };
  wrapped.add(api);
  return api;
}

export function attachBufferUploadHook(program) {
  const deploy=program.commands.find(command=>command.name()==='deploy');
  if(!deploy||typeof deploy.hook!=='function')throw Error('netlify_deploy_hook_unavailable');
  // createDeployCommand registered BaseCommand.init first. Appending to that
  // command's public Commander hook list preserves initialization and ordering.
  deploy.hook('preAction',(_parent,command)=>bufferFileUploads(command.netlify?.api));
  return program;
}

export async function pinnedCliRoot(root=DEFAULT_CLI_ROOT) {
  const resolved=await fs.realpath(root);
  const pkg=JSON.parse(await fs.readFile(path.join(resolved,'package.json'),'utf8'));
  if(pkg.name!=='netlify-cli'||pkg.version!==PINNED_CLI_VERSION)throw Error('netlify_cli_version_mismatch');
  return resolved;
}

export async function runOfficialDeploy(argv=process.argv,root=DEFAULT_CLI_ROOT) {
  if(argv[2]!=='deploy')throw Error('netlify_buffer_wrapper_requires_deploy');
  const cli=await pinnedCliRoot(root);
  const {createMainCommand}=await import(pathToFileURL(path.join(cli,'dist/commands/main.js')).href);
  const {runProgram}=await import(pathToFileURL(path.join(cli,'dist/utils/run-program.js')).href);
  const program=attachBufferUploadHook(createMainCommand());
  try {await runProgram(program,argv);program.onEnd();}
  catch(error) {program.onEnd(error);}
}

if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href) {
  await runOfficialDeploy();
}
