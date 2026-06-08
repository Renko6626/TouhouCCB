/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_APP_TITLE?: string
  readonly VITE_CASDOOR_URL?: string
  readonly VITE_CASDOOR_CLIENT_ID?: string
  readonly VITE_CASDOOR_ORG?: string
  readonly VITE_CASDOOR_APP?: string
  readonly VITE_CLIENT_TOKEN_SECRET?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
