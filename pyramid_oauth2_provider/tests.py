#
# Copyright (c) Elliot Peele <elliot@bentlogic.net>
#
# This program is distributed under the terms of the MIT License as found
# in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/mit-license.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the MIT License for full details.
#

import base64
import unittest
import transaction
from urlparse import urlparse
from urlparse import parse_qsl

from sqlalchemy import create_engine

from zope.interface import implementer

from pyramid import testing
from pyramid.response import Response

from . import jsonerrors
from .views import oauth2_token
from .views import oauth2_authorize
from .models import DBSession
from .models import Oauth2Token
from .models import Oauth2Client
from .models import Oauth2Code
from .models import Oauth2RedirectUri
from .models import initialize_sql
from .interfaces import IAuthCheck

_auth_value = None
@implementer(IAuthCheck)
class AuthCheck(object):
    def checkauth(self, username, password):
        return _auth_value

_redirect_uri = None


class TestCase(unittest.TestCase):
    def setUp(self):
        self.config = testing.setUp()
        self.config.registry.registerUtility(AuthCheck, IAuthCheck)

        engine = create_engine('sqlite://')
        initialize_sql(engine, self.config)

        self.auth = 1

        self.redirect_uri = 'http://localhost'

    def _get_auth(self):
        global _auth_value
        return _auth_value

    def _set_auth(self, value):
        global _auth_value
        _auth_value = value

    auth = property(_get_auth, _set_auth)

    def _get_redirect_uri(self):
        global _redirect_uri
        return _redirect_uri

    def _set_redirect_uri(self, uri):
        global _redirect_uri
        _redirect_uri = uri

    redirect_uri = property(_get_redirect_uri, _set_redirect_uri)

    def tearDown(self):
        DBSession.remove()
        testing.tearDown()

    @classmethod
    def tearDownClass(cls):
        """ Extra DBSession remove"""
        DBSession.remove()

    def getAuthHeader(self, username, password, scheme='Basic'):
        return {'Authorization': '%s %s'
            % (scheme, base64.b64encode('%s:%s' % (username, password)))}


class TestAuthorizeEndpoint(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.client = self._create_client()
        self.request = self._create_request()
        self.config.testing_securitypolicy(self.auth)

    def tearDown(self):
        TestCase.tearDown(self)
        self.client = None
        self.request = None

    def _create_client(self):
        with transaction.manager:
            client = Oauth2Client()
            DBSession.add(client)
            client_id = client.client_id

            redirect_uri = Oauth2RedirectUri(client, self.redirect_uri)
            DBSession.add(redirect_uri)

        client = DBSession.query(Oauth2Client).filter_by(client_id=client_id).first()
        return client

    def _create_request(self):
        data = {
            'response_type': 'code',
            'client_id': self.client.client_id
        }

        request = testing.DummyRequest(params=data)
        request.scheme = 'https'

        return request

    def _create_implicit_request(self):
        data = {
            'response_type': 'token',
            'client_id': self.client.client_id
        }

        request = testing.DummyRequest(post=data)
        request.scheme = 'https'

        return request

    def _process_view(self):
        with transaction.manager:
            token = oauth2_authorize(self.request)
        return token

    def _validate_authcode_response(self, response):
        self.failUnless(isinstance(response, Response))
        self.failUnlessEqual(response.status_int, 302)

        redirect = urlparse(self.redirect_uri)
        location = urlparse(response.location)
        self.failUnlessEqual(location.scheme, redirect.scheme)
        self.failUnlessEqual(location.hostname, redirect.hostname)
        self.failUnlessEqual(location.path, redirect.path)
        self.failIf(location.fragment)

        params = dict(parse_qsl(location.query))

        self.failUnless('code' in params)

        dbauthcodes = DBSession.query(Oauth2Code).filter_by(
            authcode=params.get('code')).all()

        self.failUnless(len(dbauthcodes) == 1)

    def testAuthCodeRequest(self):
        response = self._process_view()
        self._validate_authcode_response(response)

    def testInvalidScheme(self):
        self.request.scheme = 'http'
        response = self._process_view()
        self.failUnless(isinstance(response, jsonerrors.HTTPBadRequest))

    def testDisableSchemeCheck(self):
        self.request.scheme = 'http'
        self.config.get_settings()['oauth2_provider.require_ssl'] = False
        response = self._process_view()
        self._validate_authcode_response(response)

    def testNoClientCreds(self):
        self.request.params.pop('client_id')
        response = self._process_view()
        self.failUnless(isinstance(response, jsonerrors.HTTPBadRequest))

    def testNoResponseType(self):
        self.request.params.pop('response_type')
        response = self._process_view()
        self.failUnless(isinstance(response, jsonerrors.HTTPBadRequest))

    def testRedirectUriSupplied(self):
        self.request.params['redirect_uri'] = self.redirect_uri
        response = self._process_view()
        self._validate_authcode_response(response)

    def testMultipleRedirectUrisUnspecified(self):
        with transaction.manager:
            redirect_uri = Oauth2RedirectUri(self.client, 'https://otherhost.com')
            DBSession.add(redirect_uri)
        response = self._process_view()
        self.failUnless(isinstance(response, jsonerrors.HTTPBadRequest))

    def testMultipleRedirectUrisSpecified(self):
        with transaction.manager:
            redirect_uri = Oauth2RedirectUri(self.client, 'https://otherhost.com')
            DBSession.add(redirect_uri)
        self.request.params['redirect_uri'] = 'https://otherhost.com'
        self.redirect_uri = 'https://otherhost.com'
        response = self._process_view()
        self._validate_authcode_response(response)

    def testRetainRedirectQueryComponent(self):
        uri = 'https://otherhost.com/and/path?some=value'
        with transaction.manager:
            redirect_uri = Oauth2RedirectUri(
                self.client, uri)
            DBSession.add(redirect_uri)
        self.request.params['redirect_uri'] = uri
        self.redirect_uri = uri
        response = self._process_view()
        self._validate_authcode_response(response)

        parts = urlparse(response.location)
        params = dict(parse_qsl(parts.query))

        self.failUnless('some' in params)
        self.failUnlessEqual(params['some'], 'value')

    def testState(self):
        state_value = 'testing'
        self.request.params['state'] = state_value
        response = self._process_view()
        self._validate_authcode_response(response)
        parts = urlparse(response.location)
        params = dict(parse_qsl(parts.query))
        self.failUnless('state' in params)
        self.failUnlessEqual(state_value, params['state'])


class TestImplicitGrant(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.client = self._create_client()
        self.request = self._create_request()
        self.config.testing_securitypolicy(self.auth)

    def tearDown(self):
        TestCase.tearDown(self)
        self.client = None
        self.request = None

    def _create_client(self):
        with transaction.manager:
            client = Oauth2Client()
            DBSession.add(client)
            client_id = client.client_id

            redirect_uri = Oauth2RedirectUri(client, self.redirect_uri)
            DBSession.add(redirect_uri)

        client = DBSession.query(Oauth2Client).filter_by(
            client_id=client_id).first()
        return client

    def _create_request(self):
        data = {
            'response_type': 'token',
            'client_id': self.client.client_id,
            'redirect_uri': self.redirect_uri,
            'state': 'test'
        }

        request = testing.DummyRequest(params=data)
        request.scheme = 'https'

        return request

    def _process_view(self):
        with transaction.manager:
            response = oauth2_authorize(self.request)
        return response

    def _validate_response(self, response):
        self.failUnless(isinstance(response, Response))
        self.failUnlessEqual(response.status_int, 302)

        redirect = urlparse(self.redirect_uri)
        location = urlparse(response.location)
        self.failUnlessEqual(location.scheme, redirect.scheme)
        self.failUnlessEqual(location.hostname, redirect.hostname)
        self.failUnlessEqual(location.path, redirect.path)
        self.failUnless(location.fragment)

        params = dict(parse_qsl(location.fragment))

        self.failUnless('access_token' in params)
        self.failUnlessEqual(params.get('token_type').lower(), 'bearer')

        token = DBSession.query(Oauth2Token).filter_by(
            access_token=params.get('access_token')).all()

        self.failUnless(len(token) == 1)

    def testImplicitGrant(self):
        response = self._process_view()
        self._validate_response(response)


class TestTokenEndpoint(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.client = self._create_client()
        self.request = self._create_request()

    def tearDown(self):
        TestCase.tearDown(self)
        self.client = None
        self.request = None

    def _create_client(self):
        with transaction.manager:
            client = Oauth2Client()
            DBSession.add(client)
            client_id = client.client_id

        client = DBSession.query(Oauth2Client).filter_by(
            client_id=client_id).first()
        return client

    def _create_request(self):
        headers = self.getAuthHeader(
            self.client.client_id,
            self.client.client_secret)

        data = {
            'grant_type': 'password',
            'username': 'john',
            'password': 'foo',
        }

        request = testing.DummyRequest(post=data, headers=headers)
        request.scheme = 'https'

        return request

    def _create_refresh_token_request(self, refresh_token, user_id):
        headers = self.getAuthHeader(
            self.client.client_id,
            self.client.client_secret)

        data = {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'user_id': str(user_id),
        }

        request = testing.DummyRequest(post=data, headers=headers)
        request.scheme = 'https'

        return request

    def _process_view(self):
        with transaction.manager:
            token = oauth2_token(self.request)
        return token

    def _validate_token(self, token):
        self.failUnless(isinstance(token, dict))
        self.failUnlessEqual(token.get('user_id'), self.auth)
        self.failUnlessEqual(token.get('expires_in'), 3600)
        self.failUnlessEqual(token.get('token_type'), 'bearer')
        self.failUnlessEqual(len(token.get('access_token')), 64)
        self.failUnlessEqual(len(token.get('refresh_token')), 64)
        self.failUnlessEqual(len(token), 5)

        dbtoken = DBSession.query(Oauth2Token).filter_by(
            access_token=token.get('access_token')).first()

        self.failUnlessEqual(dbtoken.user_id, token.get('user_id'))
        self.failUnlessEqual(dbtoken.expires_in, token.get('expires_in'))
        self.failUnlessEqual(dbtoken.access_token, token.get('access_token'))
        self.failUnlessEqual(dbtoken.refresh_token, token.get('refresh_token'))

    def testTokenRequest(self):
        self.auth = 500
        token = self._process_view()
        self._validate_token(token)

    def testInvalidMethod(self):
        self.request.method = 'GET'
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPMethodNotAllowed))

    def testInvalidScheme(self):
        self.request.scheme = 'http'
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testDisableSchemeCheck(self):
        self.request.scheme = 'http'
        self.config.get_settings()['oauth2_provider.require_ssl'] = False
        token = self._process_view()
        self._validate_token(token)

    def testNoClientCreds(self):
        self.request.headers = {}
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPUnauthorized))

    def testInvalidClientCreds(self):
        self.request.headers = self.getAuthHeader(
            self.client.client_id, 'abcde')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testInvalidGrantType(self):
        self.request.POST['grant_type'] = 'foo'
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testCacheHeaders(self):
        self._process_view()
        self.failUnlessEqual(
            self.request.response.headers.get('Cache-Control'), 'no-store')
        self.failUnlessEqual(
            self.request.response.headers.get('Pragma'), 'no-cache')

    def testMissingUsername(self):
        self.request.POST.pop('username')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testMissingPassword(self):
        self.request.POST.pop('password')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testFailedPassword(self):
        self.auth = False
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPUnauthorized))

    def testRefreshToken(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), token.get('user_id'))
        token = self._process_view()
        self._validate_token(token)

    def testMissingRefreshToken(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), token.get('user_id'))
        self.request.POST.pop('refresh_token')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testMissingUserId(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), token.get('user_id'))
        self.request.POST.pop('user_id')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testInvalidRefreshToken(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            'abcd', token.get('user_id'))
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPUnauthorized))

    def testRefreshInvalidClientId(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), token.get('user_id'))
        self.request.headers = self.getAuthHeader(
            '1234', self.client.client_secret)
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testUserIdMissmatch(self):
        token = self._process_view()
        self._validate_token(token)
        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), '2')
        token = self._process_view()
        self.failUnless(isinstance(token, jsonerrors.HTTPBadRequest))

    def testRevokedAccessTokenRefresh(self):
        token = self._process_view()
        self._validate_token(token)

        dbtoken = DBSession.query(Oauth2Token).filter_by(
            access_token=token.get('access_token')).first()
        dbtoken.revoke()

        self.request = self._create_refresh_token_request(
            token.get('refresh_token'), token.get('user_id'))
        token = self._process_view()
        self._validate_token(token)

    def testTimeRevokeAccessToken(self):
        token = self._process_view()
        self._validate_token(token)

        dbtoken = DBSession.query(Oauth2Token).filter_by(
            access_token=token.get('access_token')).first()
        dbtoken.expires_in = 0

        self.failUnlessEqual(dbtoken.isRevoked(), True)


class TestAuthcodeExchange(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.code = self._create_code()
        self.request = self._create_request()

    def tearDown(self):
        TestCase.tearDown(self)
        self.code = None
        self.request = None

    def _create_code(self):
        with transaction.manager:
            client = Oauth2Client()
            DBSession.add(client)

            redirect_uri = Oauth2RedirectUri(client, self.redirect_uri)
            DBSession.add(redirect_uri)

            code = Oauth2Code(client, self.auth, redirect_uri)
            DBSession.add(code)


        code = DBSession.query(Oauth2Code).all()[-1]
        return code

    def _create_request(self):
        headers = self.getAuthHeader(
            self.code.client.client_id,
            self.code.client.client_secret)

        data = {
            'grant_type': 'authorization_code',
            'code': self.code.authcode,
            'client_id': self.code.client.client_id,
            'redirect_uri': self.redirect_uri
        }

        request = testing.DummyRequest(post=data, headers=headers)
        request.scheme = 'https'

        return request

    def _process_view(self):
        with transaction.manager:
            token = oauth2_token(self.request)
        return token

    def _validate_token(self, token):
        self.failUnless(isinstance(token, dict))
        self.failUnlessEqual(token.get('user_id'), self.auth)
        self.failUnlessEqual(token.get('expires_in'), 3600)
        self.failUnlessEqual(token.get('token_type'), 'bearer')
        self.failUnlessEqual(len(token.get('access_token')), 64)
        self.failUnlessEqual(len(token.get('refresh_token')), 64)
        self.failUnlessEqual(len(token), 5)

    def testExchangeToken(self):
        token = self._process_view()
        self._validate_token(token)
