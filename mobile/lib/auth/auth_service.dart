import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';

import '../collector/collector_bridge.dart';
import '../config.dart';

class AuthException implements Exception {
  AuthException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Signed-in identity (decoded from the JWT for display only; the server verifies tokens).
class AuthUser {
  AuthUser({required this.id, this.email});
  final String id;
  final String? email;
}

/// Supabase Auth over its REST API (email + password), or locally minted development tokens.
///
/// Session storage:
///  * Android: the Kotlin SessionStore (Keystore-encrypted) is the ONLY refresher, shared
///    with the background collector, avoiding Supabase refresh-token reuse revocation.
///  * iOS / web: flutter_secure_storage, refreshed here behind a single in-flight future.
class AuthService extends ChangeNotifier {
  AuthService({http.Client? client}) : _http = client ?? http.Client();

  final http.Client _http;
  final _storage = const FlutterSecureStorage();
  AuthUser? user;
  Future<String>? _refreshing;

  bool get signedIn => user != null;
  bool get _native => CollectorBridge.isSupported;

  Future<void> restore() async {
    try {
      final token = await accessToken();
      user = _userFromJwt(token);
    } catch (_) {
      user = null;
    }
    notifyListeners();
  }

  Future<void> signIn(
    String email,
    String password, {
    bool signUp = false,
  }) async {
    if (AppConfig.isDevAuth) return _devSignIn(email);
    if (AppConfig.supabaseUrl.isEmpty || AppConfig.supabaseAnonKey.isEmpty) {
      throw AuthException(
        'SUPABASE_URL / SUPABASE_ANON_KEY not configured in this build',
      );
    }
    final path = signUp
        ? '/auth/v1/signup'
        : '/auth/v1/token?grant_type=password';
    final r = await _http.post(
      Uri.parse('${AppConfig.supabaseUrl}$path'),
      headers: _supabaseHeaders,
      body: jsonEncode({'email': email, 'password': password}),
    );
    final body = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.statusCode != 200) {
      throw AuthException(
        (body['error_description'] ?? body['msg'] ?? 'Sign-in failed')
            .toString(),
      );
    }
    if (body['access_token'] == null) {
      throw AuthException(
        'Check your email to confirm the account, then sign in.',
      );
    }
    await _saveSession(_sessionFrom(body));
  }

  Future<void> _devSignIn(String email) async {
    // Deterministic development identity per email (matches scripts/seed_demo.py).
    final uid = const Uuid().v5(Namespace.url.value, 'familypulse-dev:$email');
    final r = await _http.post(
      Uri.parse('${AppConfig.apiBaseUrl}/v1/dev/token'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'email': email, 'user_id': uid}),
    );
    if (r.statusCode != 200) {
      throw AuthException(
        'Development sign-in failed (${r.statusCode}). '
        'Is the backend running with FP_DEV_TOKEN_MINT_ENABLED=true?',
      );
    }
    final body = jsonDecode(r.body) as Map<String, dynamic>;
    await _saveSession({
      'access_token': body['access_token'],
      'refresh_token': null,
      'expires_at':
          DateTime.now()
              .add(const Duration(hours: 12))
              .millisecondsSinceEpoch ~/
          1000,
      'mode': 'dev',
      'supabase_url': null,
      'anon_key': null,
    });
  }

  Map<String, String> get _supabaseHeaders => {
    'Content-Type': 'application/json',
    'apikey': AppConfig.supabaseAnonKey,
  };

  Map<String, dynamic> _sessionFrom(Map<String, dynamic> b) => {
    'access_token': b['access_token'],
    'refresh_token': b['refresh_token'],
    'expires_at':
        b['expires_at'] ??
        (DateTime.now().millisecondsSinceEpoch ~/ 1000 +
            (b['expires_in'] as int? ?? 3600)),
    'mode': 'supabase',
    'supabase_url': AppConfig.supabaseUrl,
    'anon_key': AppConfig.supabaseAnonKey,
  };

  Future<void> _saveSession(Map<String, dynamic> s) async {
    if (_native) {
      await CollectorBridge.setSession(s);
    } else {
      await _storage.write(key: 'session', value: jsonEncode(s));
    }
    user = _userFromJwt(s['access_token'] as String);
    notifyListeners();
  }

  /// Valid access token, refreshed if needed. Throws [AuthException] if sign-in is required.
  Future<String> accessToken() async {
    if (_native) {
      try {
        return await CollectorBridge.accessToken();
      } on PlatformException catch (e) {
        if (e.code == 'auth_required') {
          user = null;
          notifyListeners();
          throw AuthException('Please sign in again');
        }
        rethrow;
      }
    }
    final raw = await _storage.read(key: 'session');
    if (raw == null) throw AuthException('Not signed in');
    final s = jsonDecode(raw) as Map<String, dynamic>;
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    if ((s['expires_at'] as int) - now > 120) {
      return s['access_token'] as String;
    }
    if (s['mode'] != 'supabase' || s['refresh_token'] == null) {
      await signOut();
      throw AuthException('Session expired');
    }
    return _refreshing ??= _refresh(s).whenComplete(() => _refreshing = null);
  }

  Future<String> _refresh(Map<String, dynamic> s) async {
    final r = await _http.post(
      Uri.parse('${s['supabase_url']}/auth/v1/token?grant_type=refresh_token'),
      headers: {
        'Content-Type': 'application/json',
        'apikey': s['anon_key'] as String,
      },
      body: jsonEncode({'refresh_token': s['refresh_token']}),
    );
    if (r.statusCode >= 400 && r.statusCode < 500) {
      await signOut();
      throw AuthException('Session expired');
    }
    if (r.statusCode != 200) {
      throw AuthException('Network problem refreshing session');
    }
    final next = _sessionFrom(jsonDecode(r.body) as Map<String, dynamic>);
    await _storage.write(key: 'session', value: jsonEncode(next));
    return next['access_token'] as String;
  }

  Future<void> signOut() async {
    if (_native) {
      await CollectorBridge.signOut(); // also cancels work and deletes queued data
    } else {
      await _storage.delete(key: 'session');
    }
    user = null;
    notifyListeners();
  }

  static AuthUser _userFromJwt(String token) {
    final parts = token.split('.');
    final payload = utf8.decode(
      base64Url.decode(base64Url.normalize(parts[1])),
    );
    final m = jsonDecode(payload) as Map<String, dynamic>;
    return AuthUser(id: m['sub'] as String, email: m['email'] as String?);
  }
}
