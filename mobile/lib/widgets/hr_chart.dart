import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';

import '../api/models.dart';

/// Splits samples into continuous segments; a new segment starts whenever consecutive
/// samples are further apart than [maxGap]. Gaps are drawn as breaks, never interpolated.
List<List<MeasurementPoint>> segmentsWithGaps(
  List<MeasurementPoint> points,
  Duration maxGap,
) {
  final sorted = [...points]..sort((a, b) => a.t.compareTo(b.t));
  final out = <List<MeasurementPoint>>[];
  for (final p in sorted) {
    if (out.isEmpty || p.t.difference(out.last.last.t) > maxGap) {
      out.add([p]);
    } else {
      out.last.add(p);
    }
  }
  return out;
}

class HeartRateChart extends StatelessWidget {
  const HeartRateChart({
    super.key,
    required this.points,
    required this.from,
    required this.to,
    this.maxGap = const Duration(minutes: 15),
  });

  final List<MeasurementPoint> points;
  final DateTime from;
  final DateTime to;
  final Duration maxGap;

  @override
  Widget build(BuildContext context) {
    if (points.isEmpty) {
      return const SizedBox(
        height: 120,
        child: Center(child: Text('No heart-rate samples in this period')),
      );
    }
    final segs = segmentsWithGaps(points, maxGap);
    double x(DateTime t) => t.difference(from).inSeconds / 3600.0;
    final spots = <FlSpot>[];
    for (final s in segs) {
      if (spots.isNotEmpty) spots.add(FlSpot.nullSpot); // explicit break
      spots.addAll(s.map((p) => FlSpot(x(p.t), p.value)));
    }
    final simulated = points.any((p) => p.simulated);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          height: 220,
          child: LineChart(
            LineChartData(
              minX: 0,
              maxX: x(to),
              minY: 40,
              maxY: 180,
              lineBarsData: [
                LineChartBarData(
                  spots: spots,
                  isCurved: false,
                  barWidth: 2,
                  color: simulated ? Colors.deepPurple : Colors.red.shade700,
                  dotData: FlDotData(show: points.length < 60),
                ),
              ],
              titlesData: FlTitlesData(
                topTitles: const AxisTitles(
                  sideTitles: SideTitles(showTitles: false),
                ),
                rightTitles: const AxisTitles(
                  sideTitles: SideTitles(showTitles: false),
                ),
                bottomTitles: AxisTitles(
                  axisNameWidget: const Text('hours in window'),
                  sideTitles: SideTitles(
                    showTitles: true,
                    reservedSize: 28,
                    interval: 3,
                  ),
                ),
              ),
            ),
          ),
        ),
        Text(
          '${segs.length - 1} gap(s) longer than ${maxGap.inMinutes} min shown as breaks'
          '${simulated ? ' · SIMULATED' : ''}',
        ),
      ],
    );
  }
}
