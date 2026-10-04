import 'package:familypulse/api/models.dart';
import 'package:familypulse/main.dart' show buildTheme;
import 'package:familypulse/ui/caregiver_screens.dart';
import 'package:familypulse/util/time_format.dart';
import 'package:familypulse/widgets/common.dart';
import 'package:familypulse/widgets/hr_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

final _now = DateTime.utc(2026, 10, 4, 6);

Map<String, dynamic> _metric(
  String metric,
  String state, {
  double? value,
  int? age,
  bool simulated = false,
  String? reason,
}) {
  final t = _now.subtract(Duration(seconds: age ?? 0)).toIso8601String();
  return {
    'metric': metric,
    'state': state,
    'reason': reason,
    'freshness_budget_seconds': 10800,
    'age_seconds': age,
    'latest': value == null
        ? null
        : {
            'value': value,
            'unit': metric == 'steps' ? 'count' : 'bpm',
            'kind': 'sample',
            'measured_at': t,
            'end_at': null,
            'received_at': _now.toIso8601String(),
            'data_origin': simulated
                ? 'simulation:familypulse'
                : 'com.fitbit.FitbitMobile',
            'device': {'model': 'Fitbit'},
            'is_simulated': simulated,
          },
  };
}

Dashboard _dashboard({
  required List<Map<String, dynamic>> metrics,
  String dataMode = 'live',
  String summary = 'No configured anomalies detected in available data',
}) => Dashboard.fromJson({
  'profile': {
    'id': 'p1',
    'display_name': 'Mum',
    'timezone': 'Asia/Kolkata',
    'data_mode': dataMode,
    'role': 'caregiver',
    'categories': ['heart', 'sleep', 'activity', 'alerts', 'connectivity'],
    'contact_phone': null,
  },
  'generated_at': _now.toIso8601String(),
  'data_mode': dataMode,
  'banner': dataMode == 'simulation'
      ? 'SIMULATED DATA - not real measurements'
      : null,
  'summary_text': summary,
  'health_thresholds_configured': false,
  'metrics': metrics,
  'unavailable_metrics': {'oxygen_saturation': 'not exported'},
  'daily': [],
  'collector': null,
  'open_alerts': [],
  'insights': [],
});

Future<void> _pump(WidgetTester t, Dashboard d) async {
  await t.pumpWidget(
    MaterialApp(
      theme: buildTheme(),
      home: Scaffold(
        body: DashboardView(
          dashboard: d,
          heartRate: const [],
          chartFrom: _now.subtract(const Duration(hours: 24)),
          chartTo: _now,
        ),
      ),
    ),
  );
}

String _stateText(WidgetTester t, String metric) =>
    t.widget<Text>(find.byKey(Key('state-$metric'))).data!;

void main() {
  setUpAll(initTimezones);

  testWidgets('stale value is labelled NOT CURRENT and struck through', (
    t,
  ) async {
    await _pump(
      t,
      _dashboard(
        metrics: [_metric('heart_rate', 'stale', value: 71, age: 4 * 3600)],
      ),
    );
    expect(_stateText(t, 'heart_rate'), 'NOT CURRENT');
    final value = t.widget<Text>(find.text('71 bpm'));
    expect(value.style?.decoration, TextDecoration.lineThrough);
    expect(find.textContaining('4 h ago'), findsOneWidget);
    expect(find.text('Current'), findsNothing);
  });

  testWidgets('fresh value is shown as current with source', (t) async {
    await _pump(
      t,
      _dashboard(
        metrics: [_metric('heart_rate', 'fresh', value: 72, age: 300)],
      ),
    );
    expect(_stateText(t, 'heart_rate'), 'Current');
    expect(
      t.widget<Text>(find.text('72 bpm')).style?.decoration,
      isNot(TextDecoration.lineThrough),
    );
    expect(find.textContaining('com.fitbit.FitbitMobile'), findsOneWidget);
  });

  testWidgets(
    'unsupported, permission-denied and waiting states render distinctly',
    (t) async {
      await _pump(
        t,
        _dashboard(
          metrics: [
            _metric('hrv_rmssd', 'unsupported', reason: 'Not collected'),
            _metric(
              'sleep',
              'permission_denied',
              reason: 'Health Connect read permission not granted.',
            ),
            _metric('steps', 'waiting_for_data'),
          ],
        ),
      );
      expect(_stateText(t, 'hrv_rmssd'), 'Not available');
      expect(_stateText(t, 'sleep'), 'Permission not granted');
      expect(_stateText(t, 'steps'), 'Waiting for data');
      expect(find.textContaining('bpm'), findsNothing); // no fabricated values
    },
  );

  testWidgets(
    'simulation profile shows banner and SIMULATED badge, never LIVE',
    (t) async {
      await _pump(
        t,
        _dashboard(
          dataMode: 'simulation',
          metrics: [
            _metric(
              'heart_rate',
              'fresh',
              value: 130,
              age: 60,
              simulated: true,
            ),
          ],
        ),
      );
      expect(find.byKey(const Key('simulation-banner')), findsOneWidget);
      expect(find.text('SIMULATED'), findsOneWidget);
      expect(find.text('LIVE'), findsNothing);
      expect(find.textContaining('Source: SIMULATION'), findsOneWidget);
    },
  );

  testWidgets('live profile has no simulation banner and never says healthy', (
    t,
  ) async {
    await _pump(
      t,
      _dashboard(metrics: [_metric('heart_rate', 'fresh', value: 70, age: 60)]),
    );
    expect(find.byKey(const Key('simulation-banner')), findsNothing);
    expect(find.text('LIVE'), findsOneWidget);
    expect(find.textContaining('healthy', findRichText: true), findsNothing);
    expect(
      t.widget<Text>(find.byKey(const Key('summary-text'))).data,
      'No configured anomalies detected in available data',
    );
  });

  test('chart segments break at gaps instead of interpolating', () {
    MeasurementPoint p(int minute) => MeasurementPoint(
      t: _now.add(Duration(minutes: minute)),
      value: 70,
      origin: 'x',
      simulated: false,
    );
    final segs = segmentsWithGaps([
      p(0),
      p(5),
      p(10),
      p(60),
      p(65),
      p(200),
    ], const Duration(minutes: 15));
    expect(segs.map((s) => s.length).toList(), [3, 2, 1]);
  });

  test('dual time format labels wearer and viewer zones', () {
    final s = formatDual(
      DateTime.utc(2026, 10, 4, 8, 35),
      'Asia/Kolkata',
      toLocal: (d) => d,
    ); // viewer in UTC
    expect(s, contains('14:05 IST'));
    expect(s, contains('08:35'));
  });

  test('state styles never present stale as current', () {
    expect(stateStyle('stale').label, isNot(stateStyle('fresh').label));
  });
}
