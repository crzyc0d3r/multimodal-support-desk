/** User-scoped SDK operations for the optional PostgreSQL schema; no admin key. */
import { createClient } from '@insforge/sdk';

function unwrap(result) {
  if (result.error) throw new Error(result.error.message || 'InsForge operation failed');
  return result.data;
}

export function createSupportBackend(baseUrl, accessToken) {
  const url = new URL(baseUrl);
  const loopback = ['localhost','127.0.0.1','[::1]'].includes(url.hostname);
  if (url.username || url.password || url.search || url.hash ||
      !(url.protocol === 'https:' || (url.protocol === 'http:' && loopback))) {
    throw new Error('Use a credential-free HTTPS URL, or loopback for local development');
  }
  const client = createClient({baseUrl, accessToken});
  const uuid = value => {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error('Company must be a UUID');
    return value;
  };
  return {
    async currentUser() { return unwrap(await client.auth.getCurrentUser()); },
    async documents(company) {
      return unwrap(await client.database.from('support_documents').select('*').eq('company_id',uuid(company)));
    },
    async tickets(company) {
      return unwrap(await client.database.from('support_tickets').select('*').eq('company_id',uuid(company)).order('created_at',{ascending:false}));
    },
    async search(company, kind, embedding, limit=8) {
      if (!['text','image'].includes(kind) || embedding.length!==768 || !embedding.every(Number.isFinite)) throw new Error('Invalid search input');
      return unwrap(await client.database.rpc('match_support_chunks', {
        query_embedding:embedding,requested_company:uuid(company),requested_kind:kind,result_limit:limit
      }));
    },
    async storeDocument(company, file) {
      const key=`${uuid(company)}/${crypto.randomUUID()}/${file.name.replace(/[^a-z0-9._-]/gi,'_')}`;
      return unwrap(await client.storage.from('support-documents').upload(key,file));
    },
    async downloadDocument(company, key) {
      if (!key.startsWith(uuid(company)+'/')) throw new Error('Storage key belongs to another company');
      return unwrap(await client.storage.from('support-documents').download(key));
    }
  };
}
