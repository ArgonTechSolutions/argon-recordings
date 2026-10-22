"""Use Authlib's real PKCE and signed ID-token verification with disposable RSA keys."""
import json
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
import pytest

from argon_recordings.api import Settings, create_app
from argon_recordings import account_routes
from test_accounts import PASSWORD, session, login


@pytest.fixture
def oidc(tmp_path, monkeypatch):
    root = tmp_path / 'auth'
    root.mkdir()
    config = root / 'oidc.json'
    config.write_text(json.dumps(dict(issuer='https://issuer.example',client_id='recordings',client_secret='synthetic-oidc-secret')))
    config.chmod(0o600)
    app = create_app(Settings(tmp_path,auth_mode='accounts',public_url='https://recordings.example'))
    key = RSAKey.generate_key(parameters={'kid':'synthetic-key'})
    claims_override = {}
    original = account_routes.create_oidc_client
    def client(config):
        value = original(config)
        metadata = dict(issuer=config['issuer'],authorization_endpoint=config['issuer']+'/authorize',token_endpoint=config['issuer']+'/token',jwks_uri=config['issuer']+'/keys',id_token_signing_alg_values_supported=['RS256'])
        async def load():
            return metadata
        async def keys(force=False):
            return {'keys':[key.as_dict()]}
        async def token(**kwargs):
            assert kwargs['redirect_uri'] == 'https://recordings.example/api/auth/oidc/callback'
            with app.state.store.db() as db:
                flow = db.execute('SELECT nonce,verifier FROM oidc_flows ORDER BY expires DESC LIMIT 1').fetchone()
            # callback consumes the flow before exchange; retain its values at start.
            flow = flow or latest_flow[0]
            assert kwargs['code_verifier'] == flow['verifier']
            claims = dict(iss='https://issuer.example',sub='external-subject',aud='recordings',iat=int(time.time()),exp=int(time.time())+300,
                          nonce=flow['nonce'],email='new@example.com',email_verified=True,name='OIDC user')
            claims.update(claims_override)
            return {'access_token':'synthetic-access','token_type':'Bearer','id_token':jwt.encode({'alg':'RS256','kid':'synthetic-key'},claims,key)}
        value.load_server_metadata = load
        value.fetch_jwk_set = keys
        value.fetch_access_token = token
        return value
    latest_flow = [None]
    monkeypatch.setattr(account_routes,'create_oidc_client',client)
    with TestClient(app,base_url='https://recordings.example') as http:
        uid = app.state.accounts.bootstrap('owner@example.com','Owner',PASSWORD)
        yield app,http,uid,claims_override,latest_flow


def start(app, http, latest_flow, invitation=None, link=False):
    response = http.post('/api/auth/oidc/start',json={'invitation':invitation,'link':link},headers=session(http))
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()['url']).query)
    assert query['code_challenge_method'] == ['S256']
    with app.state.store.db() as db:
        latest_flow[0] = dict(db.execute('SELECT * FROM oidc_flows ORDER BY expires DESC LIMIT 1').fetchone())
    return query['state'][0]


def callback(http, state):
    return http.get('/api/auth/oidc/callback',params={'state':state,'code':'synthetic-code'},follow_redirects=False,
                    headers={'Sec-Fetch-Site':'cross-site'})


def test_oidc_invitation_signed_claims_pkce_replay_and_cookie_binding(oidc):
    app,http,uid,overrides,latest = oidc
    invite = app.state.accounts.invite(uid,'new@example.com')
    state = start(app,http,latest,invitation=invite)
    response = callback(http,state)
    assert response.status_code == 303 and response.headers['location'] == '/'
    assert http.get('/api/auth/session').json()['user']['email'] == 'new@example.com'
    assert callback(http,state).headers['location'] == '/?auth_error=oidc'
    # A valid callback in a browser that never initiated it is rejected.
    state = start(app,http,latest)
    with TestClient(app,base_url='https://recordings.example') as stranger:
        assert callback(stranger,state).headers['location'] == '/?auth_error=oidc'


@pytest.mark.parametrize('change',[{'nonce':'wrong'},{'aud':'different-client'},{'iss':'https://evil.example'}, {'exp':1}, {'email_verified':False}])
def test_oidc_invalid_claims_never_create_account(oidc, change):
    app,http,uid,overrides,latest = oidc
    overrides.update(change)
    invitation = app.state.accounts.invite(uid,'new@example.com')
    state = start(app,http,latest,invitation)
    response = callback(http,state)
    assert response.headers['location'] == '/?auth_error=oidc'
    assert http.get('/api/auth/session').json()['user'] is None
    with app.state.store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_oidc_does_not_autolink_email_and_explicit_link_enables_future_sign_in(oidc):
    app,http,uid,overrides,latest = oidc
    overrides['email'] = 'owner@example.com'
    state = start(app,http,latest)
    assert callback(http,state).headers['location'] == '/?auth_error=oidc'
    login(http)
    state = start(app,http,latest,link=True)
    assert callback(http,state).headers['location'] == '/'
    with app.state.store.db() as db:
        assert db.execute('SELECT user_id FROM external_identities').fetchone()[0] == uid
    http.post('/api/auth/logout',headers=session(http))
    state = start(app,http,latest)
    assert callback(http,state).headers['location'] == '/'
    assert http.get('/api/auth/session').json()['user']['id'] == uid


def test_callback_revalidates_https_before_sending_client_credentials(oidc, monkeypatch):
    app,http,uid,overrides,latest = oidc
    invitation = app.state.accounts.invite(uid,'new@example.com')
    state = start(app,http,latest,invitation)
    original = account_routes.create_oidc_client
    sent = []
    def changed(config):
        client = original(config)
        load = client.load_server_metadata
        async def metadata():
            result = await load()
            result['token_endpoint'] = 'http://insecure.example/token'
            return result
        async def exchange(**kwargs):
            sent.append(True)
            raise AssertionError('Credentials must not be sent to this endpoint')
        client.load_server_metadata = metadata
        client.fetch_access_token = exchange
        return client
    monkeypatch.setattr(account_routes,'create_oidc_client',changed)
    assert callback(http,state).headers['location'] == '/?auth_error=oidc'
    assert sent == []
