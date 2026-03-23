import uuid, urllib.parse
from typing import List, Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
try:
    import msal
except ModuleNotFoundError:  # pragma: no cover - dependencia opcional en entornos sin SSO
    msal = None

#Para usar la clase de auth SSO contra el tenant, debéis incorporar en la firma de cada endpoint lo siguiente: user=Depends(auth.require_user)
class EntraOIDC:
    """
    Autenticación OIDC con Microsoft Entra ID para FastAPI.
    - Expone rutas /login, /logout y el callback (por defecto /auth/callback).
    - Proporciona dependency require_user() para proteger endpoints.
    - Guarda en sesión: user (id_token_claims) y access_token (si se pidió un scope de recurso).
    """
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        redirect_path: str = "/auth/callback",
        scopes: Optional[List[str]] = None,  # p.ej. [] o ["https://graph.microsoft.com/User.Read"]
        session_key_user: str = "user",
        session_key_access_token: str = "access_token",
        session_key_state: str = "state",
        session_key_next: str = "next",
        prompt: str = "select_account",
    ):
        if not all([tenant_id, client_id, client_secret, base_url]):
            raise RuntimeError("Faltan tenant_id/client_id/client_secret/base_url en EntraOIDC")

        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.base_url = base_url.rstrip("/")
        self.redirect_path = redirect_path
        self.redirect_uri = f"{self.base_url}{self.redirect_path}"
        self.scopes = scopes or []  # no incluir 'openid/profile/offline_access'
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.session_key_user = session_key_user
        self.session_key_access_token = session_key_access_token
        self.session_key_state = session_key_state
        self.session_key_next = session_key_next
        self.prompt = prompt

        self.router = APIRouter()
        self._mount_routes()

    # ---------- Internos ----------
    def _msal_app(self):
        if msal is None:
            raise RuntimeError("msal no está instalado; el flujo OIDC de Entra está deshabilitado en este entorno.")
        return msal.ConfidentialClientApplication(
            self.client_id, authority=self.authority, client_credential=self.client_secret
        )

    def _auth_url(self, state: str) -> str:
        return self._msal_app().get_authorization_request_url(
            self.scopes, state=state, redirect_uri=self.redirect_uri, prompt=self.prompt
        )

    # ---------- Dependency ----------
    def require_user(self, req: Request):
        user = req.session.get(self.session_key_user)
        if user:
            return user
        # redirige a /login con ?next=<ruta_actual>
        next_url = urllib.parse.quote(self._current_path_with_query(req), safe="")
        login = f"/login?next={next_url}"
        raise HTTPException(status_code=307, detail="redirect", headers={"Location": login})

    # ---------- Rutas ----------
    def _mount_routes(self):
        @self.router.get("/login")
        def login(request: Request, next: str = "/"):
            state = uuid.uuid4().hex
            request.session[self.session_key_state] = state
            request.session[self.session_key_next] = next if next else "/"
            try:
                return RedirectResponse(self._auth_url(state))
            except Exception as e:
                return HTMLResponse(f"<h3>Configuración OIDC incompleta: {e}</h3>", status_code=500)

        @self.router.get(self.redirect_path)
        def callback(request: Request, state: str = "", code: str = ""):
            if not state or state != request.session.get(self.session_key_state):
                return HTMLResponse("<h3>State inválido</h3>", status_code=400)
            try:
                result = self._msal_app().acquire_token_by_authorization_code(
                    code, self.scopes, redirect_uri=self.redirect_uri
                )
            except Exception as e:
                return HTMLResponse(f"<h3>Error autenticando: {e}</h3>", status_code=500)

            if "id_token_claims" not in result:
                err = result.get("error_description") or result
                return HTMLResponse(f"<h3>Login fallido</h3><pre>{err}</pre>", status_code=401)

            claims = result["id_token_claims"]

            # 🔴 ¡CLAVE!: guarda un payload pequeño en la cookie de sesión
            request.session[self.session_key_user] = {
                "oid": claims.get("oid"),
                "name": claims.get("name") or claims.get("preferred_username"),
                "upn": claims.get("preferred_username"),
                "tid": claims.get("tid"),
            }
            # ❌ No metas "access_token" en la cookie; si alguna vez lo necesitas, guárdalo server-side.

            next_url = request.session.get(self.session_key_next, "/") or "/"
            return RedirectResponse(next_url)

        @self.router.get("/logout")
        def logout(request: Request):
            request.session.clear()
            post = urllib.parse.quote(self.base_url, safe="")
            return RedirectResponse(f"{self.authority}/oauth2/v2.0/logout?post_logout_redirect_uri={post}")

    # ---------- Utilidades ----------
    @staticmethod
    def _current_path_with_query(req: Request) -> str:
        qp = ("?" + req.url.query) if req.url.query else ""
        return f"{req.url.path}{qp}"