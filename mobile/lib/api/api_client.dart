import 'dart:convert';

import 'package:http/http.dart' as http;

import '../auth/auth_service.dart';
import '../config.dart';
import 'models.dart';

class ApiException implements Exception {
  ApiException(this.status, this.detail);
  final int status;
  final String detail;
  @override
  String toString() => 'Server error $status: $detail';
}

class ApiClient {
  ApiClient(this.auth, {http.Client? client, String? baseUrl})
    : _http = client ?? http.Client(),
      baseUrl = baseUrl ?? AppConfig.apiBaseUrl;

  final AuthService auth;
  final http.Client _http;
  final String baseUrl;

  Future<dynamic> _req(String method, String path, {Object? body}) async {
    final token = await auth.accessToken();
    final req = http.Request(method, Uri.parse('$baseUrl$path'))
      ..headers['Authorization'] = 'Bearer $token'
      ..headers['Content-Type'] = 'application/json';
    if (body != null) req.body = jsonEncode(body);
    final res = await http.Response.fromStream(await _http.send(req));
    if (res.statusCode == 204) return null;
    final decoded = res.body.isEmpty ? null : jsonDecode(res.body);
    if (res.statusCode >= 400) {
      final detail = decoded is Map ? decoded['detail'] : null;
      throw ApiException(
        res.statusCode,
        detail is String ? detail : jsonEncode(detail),
      );
    }
    return decoded;
  }

  Future<Map<String, dynamic>> me() async =>
      await _req('GET', '/v1/me') as Map<String, dynamic>;
  Future<void> updateMe({String? displayName, String? timezone}) => _req(
    'PATCH',
    '/v1/me',
    body: {'display_name': ?displayName, 'timezone': ?timezone},
  );

  Future<List<HealthProfileInfo>> healthProfiles() async =>
      ((await _req('GET', '/v1/health-profiles')) as List)
          .map((e) => HealthProfileInfo.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<HealthProfileInfo> createHealthProfile({
    required String displayName,
    required String dataMode,
    String? phone,
  }) async => HealthProfileInfo.fromJson(
    await _req(
      'POST',
      '/v1/health-profiles',
      body: {
        'display_name': displayName,
        'data_mode': dataMode,
        'timezone': 'Asia/Kolkata',
        if (phone != null && phone.isNotEmpty) 'contact_phone': phone,
      },
    ) as Map<String, dynamic>,
  );

  Future<void> deleteMyAccountData() => _req('DELETE', '/v1/me');

  Future<void> deleteHealthProfile(String id) =>
      _req('DELETE', '/v1/health-profiles/$id');

  Future<Map<String, dynamic>> createInvitation(
    String profileId,
    List<String> categories,
  ) async => await _req(
    'POST',
    '/v1/health-profiles/$profileId/invitations',
    body: {'categories': categories},
  ) as Map<String, dynamic>;

  Future<Grant> acceptInvitation(String token) async => Grant.fromJson(
    await _req('POST', '/v1/invitations/accept', body: {'token': token})
        as Map<String, dynamic>,
  );

  Future<List<Grant>> grants(String profileId) async =>
      ((await _req('GET', '/v1/health-profiles/$profileId/grants')) as List)
          .map((e) => Grant.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<void> confirmGrant(String grantId) =>
      _req('POST', '/v1/grants/$grantId/confirm');
  Future<void> revokeGrant(String grantId) =>
      _req('DELETE', '/v1/grants/$grantId');

  Future<Map<String, dynamic>> registerCollector(
    String profileId,
    String installationId,
    Map<String, dynamic> capabilities,
  ) async => await _req(
    'POST',
    '/v1/collector-devices',
    body: {
      'health_profile_id': profileId,
      'installation_id': installationId,
      'platform': 'android',
      'capabilities': capabilities,
    },
  ) as Map<String, dynamic>;

  Future<void> disableCollector(String deviceId) =>
      _req('DELETE', '/v1/collector-devices/$deviceId');

  Future<Map<String, dynamic>> ingest(
    String profileId,
    Map<String, dynamic> batch,
  ) async =>
      await _req('POST', '/v1/health-profiles/$profileId/ingest', body: batch)
          as Map<String, dynamic>;

  Future<void> heartbeat(String deviceId, Map<String, dynamic> body) =>
      _req('POST', '/v1/collector-devices/$deviceId/heartbeat', body: body);

  Future<void> checkIn(String profileId) =>
      _req('POST', '/v1/health-profiles/$profileId/check-ins');

  Future<Dashboard> dashboard(String profileId) async => Dashboard.fromJson(
    await _req('GET', '/v1/health-profiles/$profileId/dashboard')
        as Map<String, dynamic>,
  );

  Future<List<MeasurementPoint>> measurements(
    String profileId,
    String metric,
    DateTime from,
    DateTime to,
  ) async {
    final out = <MeasurementPoint>[];
    String? cursor;
    do {
      final q =
          'metric=$metric&from=${Uri.encodeQueryComponent(from.toUtc().toIso8601String())}'
          '&to=${Uri.encodeQueryComponent(to.toUtc().toIso8601String())}&limit=2000'
          '${cursor != null ? '&cursor=$cursor' : ''}';
      final r = await _req(
        'GET',
        '/v1/health-profiles/$profileId/measurements?$q',
      ) as Map<String, dynamic>;
      out.addAll(
        (r['points'] as List).map(
          (e) => MeasurementPoint.fromJson(e as Map<String, dynamic>),
        ),
      );
      cursor = r['next_cursor'] as String?;
    } while (cursor != null && out.length < 20000);
    return out;
  }

  Future<List<AlertItem>> alerts(String profileId) async =>
      ((await _req('GET', '/v1/health-profiles/$profileId/alerts')) as List)
          .map((e) => AlertItem.fromJson(e as Map<String, dynamic>))
          .toList();

  Future<AlertItem> alert(String alertId) async => AlertItem.fromJson(
    await _req('GET', '/v1/alerts/$alertId') as Map<String, dynamic>,
  );

  Future<AlertItem> acknowledge(String alertId) async => AlertItem.fromJson(
    await _req('POST', '/v1/alerts/$alertId/acknowledge')
        as Map<String, dynamic>,
  );

  Future<List<Map<String, dynamic>>> rules(String profileId) async =>
      ((await _req('GET', '/v1/health-profiles/$profileId/rules')) as List)
          .cast<Map<String, dynamic>>();

  Future<Map<String, dynamic>> updateRule(
    String profileId,
    String ruleId,
    Map<String, dynamic> body,
  ) async => await _req(
    'PUT',
    '/v1/health-profiles/$profileId/rules/$ruleId',
    body: body,
  ) as Map<String, dynamic>;

  Future<List<Map<String, dynamic>>> syncRuns(String profileId) async =>
      ((await _req('GET', '/v1/health-profiles/$profileId/sync-runs')) as List)
          .cast<Map<String, dynamic>>();

  Future<Map<String, dynamic>> registerPush(
    String installationId,
    String token,
    String platform,
  ) async => await _req(
    'POST',
    '/v1/push-devices',
    body: {
      'installation_id': installationId,
      'token': token,
      'platform': platform,
    },
  ) as Map<String, dynamic>;

  Future<List<Map<String, dynamic>>> pushDevices() async =>
      ((await _req('GET', '/v1/push-devices')) as List)
          .cast<Map<String, dynamic>>();

  Future<Map<String, dynamic>> testPush(String deviceId) async =>
      await _req('POST', '/v1/push-devices/$deviceId/test')
          as Map<String, dynamic>;

  Future<Map<String, dynamic>> diagnostics() async =>
      await _req('GET', '/v1/diagnostics') as Map<String, dynamic>;
}
