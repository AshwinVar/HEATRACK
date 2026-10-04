import 'dart:math';

import 'package:uuid/uuid.dart';

import '../api/api_client.dart';

/// SIMULATION ONLY. Generates clearly labelled synthetic readings for a *simulation*
/// profile (origin "simulation:familypulse"). The server refuses these for live profiles,
/// and the live Health Connect collector never calls this code.
class Simulator {
  Simulator(this.api);
  final ApiClient api;

  static const origin = 'simulation:familypulse';

  static String installationFor(String baseInstall) {
    final s = 'sim-$baseInstall';
    return s.length > 64 ? s.substring(0, 64) : s;
  }

  Future<Map<String, dynamic>> ensureDevice(
    String profileId,
    String baseInstall,
  ) => api.registerCollector(profileId, installationFor(baseInstall), {
    'health_connect': 'simulated',
    'background_read': 'simulated',
    'metrics': {
      for (final m in ['heart_rate', 'resting_heart_rate', 'steps', 'sleep'])
        m: {'supported': true, 'permission': 'granted'},
    },
  });

  /// Push [minutes] of synthetic heart rate ending now. If [elevated], values sit above
  /// a typical synthetic demo threshold (labelled; not medical guidance).
  Future<Map<String, dynamic>> pushHeartRate(
    String profileId,
    String baseInstall, {
    int minutes = 30,
    bool elevated = false,
    DateTime? now,
  }) async {
    final end = (now ?? DateTime.now()).toUtc();
    final start = end.subtract(Duration(minutes: minutes));
    final rnd = Random();
    final samples = [
      for (var i = 0; i <= minutes; i += 2)
        {
          'metric': 'heart_rate',
          'value': (elevated ? 128 : 72) + rnd.nextInt(6),
          'unit': 'bpm',
          'time': start.add(Duration(minutes: i)).toIso8601String(),
        },
    ];
    final device = await ensureDevice(profileId, baseInstall);
    final result = await api.ingest(profileId, {
      'schema_version': 1,
      'installation_id': installationFor(baseInstall),
      'batch_id': const Uuid().v4(),
      'mode': 'incremental',
      'data_mode': 'simulation',
      'records': [
        {
          'record_type': 'HeartRateRecord',
          'source_record_id': 'sim-${const Uuid().v4()}',
          'data_origin': origin,
          'record_version': end.millisecondsSinceEpoch,
          'start_time': start.toIso8601String(),
          'end_time': samples.last['time'],
          'device': {
            'type': 'SIMULATED',
            'manufacturer': 'FamilyPulse',
            'model': 'simulator',
          },
          'samples': samples,
        },
      ],
      'deletions': [],
    });
    await api.heartbeat(device['id'] as String, {
      'sync_run': {
        'started_at': end.toIso8601String(),
        'ended_at': end.toIso8601String(),
        'status': 'success',
        'trigger': 'foreground',
        'metrics_found': {'heart_rate': samples.length},
      },
    });
    return result;
  }
}
