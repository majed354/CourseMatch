import {createHash} from 'node:crypto';
import {getDatabase} from '@netlify/database';
import {getStore} from '@netlify/blobs';
import catalog from '../../equivalency-pilot/catalog.json' with {type:'json'};
import {createApi} from '../lib/api.mjs';
import {PostgresRepository} from '../lib/repository.mjs';
import {contextScope} from '../lib/security.mjs';

let api;
function runtimeEnv() {
  return Object.fromEntries(['CONTEXT','DEPLOY_ID','BRANCH','COURSEMATCH_SESSION_SECRET','COURSEMATCH_WORKER_TOKEN'].map(name => [name,globalThis.Netlify?.env?.get(name) ?? process.env[name]]));
}
export default async function(request) {
  try {
    if (!api) {
      const env = runtimeEnv(), scope = contextScope(env);
      const storeName = 'coursematch-pdfs-' + createHash('sha256').update(scope).digest('hex').slice(0,20);
      const store = getStore({name:storeName,consistency:'strong'});
      api = createApi({catalog,env,repository:new PostgresRepository(getDatabase().pool),pdfs:{
        put:(key,bytes,metadata) => store.set(key,bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset + bytes.byteLength),{metadata,onlyIfNew:true}),
        get:key => store.get(key,{type:'arrayBuffer',consistency:'strong'}),
        delete:key => store.delete(key)
      }});
    }
    return await api(request);
  } catch (error) {
    console.error('coursematch_initialization_failed',error?.name || 'Error');
    return Response.json({error:'الخدمة غير متاحة مؤقتًا؛ حاول لاحقًا'},{status:503,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
  }
}
export const config = {path:['/api/*','/pdf/*'],rateLimit:{action:'rate_limit',aggregateBy:'ip',windowLimit:120,windowSize:60}};
