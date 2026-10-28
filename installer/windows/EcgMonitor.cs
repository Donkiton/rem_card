using System;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Media;

namespace RemCard.Setup
{
    /// <summary>A decorative monitor sweep. It never displays clinical data.</summary>
    public sealed class EcgMonitor : FrameworkElement, IDisposable
    {
        public static readonly DependencyProperty StrokeBrushProperty = DependencyProperty.Register(
            "StrokeBrush", typeof(Brush), typeof(EcgMonitor),
            new FrameworkPropertyMetadata(Brushes.Turquoise, FrameworkPropertyMetadataOptions.AffectsRender));
        public Brush StrokeBrush { get { return (Brush)GetValue(StrokeBrushProperty); } set { SetValue(StrokeBrushProperty, value); } }

        public bool Animate { get; set; }
        public bool Paused { get; set; }
        private bool _subscribed;
        private bool _disposed;
        private double _elapsed;
        private double? _faultAt;
        private double _faultValue;
        private bool _pendingFailure;
        private DateTime _last;
        private const double Speed = 100;
        private const double Cycle = .82;

        public EcgMonitor()
        {
            Height = 66;
            MinHeight = 42;
            SnapsToDevicePixels = true;
            ClipToBounds = true;
            Animate = SystemParameters.ClientAreaAnimation;
            AutomationProperties.SetName(this, "Декоративная ЭКГ: комплексы P–QRS–T с бегущей развёрткой");
            Loaded += delegate { Subscribe(); };
            Unloaded += delegate { Unsubscribe(); };
            SizeChanged += delegate { InvalidateVisual(); };
        }

        private void Subscribe()
        {
            if (_subscribed || _disposed || !Animate) return;
            _last = DateTime.UtcNow;
            CompositionTarget.Rendering += OnFrame;
            _subscribed = true;
        }

        private void Unsubscribe()
        {
            if (!_subscribed) return;
            CompositionTarget.Rendering -= OnFrame;
            _subscribed = false;
        }

        private void OnFrame(object sender, EventArgs args)
        {
            DateTime now = DateTime.UtcNow;
            if (IsVisible && Animate && !Paused)
                _elapsed += Math.Min(.1, Math.Max(0, (now - _last).TotalSeconds));
            _last = now;
            if (IsVisible && Animate && !Paused) InvalidateVisual();
        }

        private static double Gaussian(double phase, double center, double spread)
        {
            double value = (phase - center) / spread;
            return Math.Exp(-.5 * value * value);
        }

        private static double Signal(double time)
        {
            double phase = ((time % Cycle) + Cycle) % Cycle / Cycle;
            return .12 * Gaussian(phase, .16, .026)
                - .13 * Gaussian(phase, .30, .010)
                + 1.02 * Gaussian(phase, .326, .009)
                - .26 * Gaussian(phase, .356, .013)
                + .29 * Gaussian(phase, .58, .057)
                + .006 * Math.Sin(time * 1.8);
        }

        private double RecordedSignal(double time)
        {
            if (!_faultAt.HasValue || time < _faultAt.Value) return Signal(time);
            double after = time - _faultAt.Value;
            return after > .16 ? 0 : _faultValue * Math.Exp(-after / .025);
        }

        public void MarkFailed()
        {
            if (_faultAt.HasValue) return;
            _pendingFailure = true;
            if (ActualWidth >= 2) CommitFailure(ActualWidth);
            AutomationProperties.SetName(this, "ЭКГ после ошибки: прежние комплексы сохраняются, дальше рисуется изолиния");
            InvalidateVisual();
        }

        private void CommitFailure(double width)
        {
            double duration = width / Speed;
            _faultAt = Animate ? _elapsed + duration : duration * 1.78 - .3;
            _faultValue = Signal(_faultAt.Value);
            _pendingFailure = false;
        }

        private static Brush WithOpacity(Brush brush, double opacity)
        {
            Brush clone = brush.Clone();
            clone.Opacity *= opacity;
            if (clone.CanFreeze) clone.Freeze();
            return clone;
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            double width = ActualWidth, height = ActualHeight;
            if (width < 2 || height < 2) return;
            if (_pendingFailure && !_faultAt.HasValue) CommitFailure(width);
            Brush color = StrokeBrush ?? Brushes.Turquoise;
            Pen minor = new Pen(WithOpacity(color, .045), .5);
            Pen major = new Pen(WithOpacity(color, .085), .5);
            for (double x = .5; x < width; x += 10) dc.DrawLine(minor, new Point(x, 0), new Point(x, height));
            for (double y = .5; y < height; y += 10) dc.DrawLine(minor, new Point(0, y), new Point(width, y));
            for (double x = .5; x < width; x += 50) dc.DrawLine(major, new Point(x, 0), new Point(x, height));
            for (double y = .5; y < height; y += 50) dc.DrawLine(major, new Point(0, y), new Point(width, y));

            double duration = width / Speed;
            double time = Animate ? _elapsed + duration : duration * 1.78;
            double head = (time * Speed) % width;
            double baseline = height * .61, amplitude = height * .43;
            StreamGeometry[] geometry = new StreamGeometry[8];
            StreamGeometryContext[] writers = new StreamGeometryContext[8];
            for (int i = 0; i < 8; i++) { geometry[i] = new StreamGeometry(); writers[i] = geometry[i].Open(); }
            Point previous = new Point();
            bool hasPrevious = false;
            try
            {
                for (double x = 0; x <= width; x += .7)
                {
                    double distance = (head - x + width) % width;
                    double sampleTime = time - distance / Speed;
                    if (sampleTime < 0 || distance > width - 18) { hasPrevious = false; continue; }
                    Point point = new Point(x, baseline - amplitude * RecordedSignal(sampleTime));
                    if (hasPrevious)
                    {
                        int band = Math.Min(7, (int)((1 - distance / width) * 8));
                        writers[band].BeginFigure(previous, false, false);
                        writers[band].LineTo(point, true, false);
                    }
                    previous = point; hasPrevious = true;
                }
            }
            finally { for (int i = 0; i < 8; i++) writers[i].Close(); }
            for (int i = 0; i < 8; i++)
            {
                geometry[i].Freeze();
                Pen pen = new Pen(WithOpacity(color, .28 + .7 * (i + 1) / 8), 1.45);
                pen.StartLineCap = PenLineCap.Round; pen.EndLineCap = PenLineCap.Round;
                dc.DrawGeometry(null, pen, geometry[i]);
            }
            if (Animate)
                dc.DrawEllipse(color, null, new Point(head, baseline - amplitude * RecordedSignal(time)), 1.65, 1.65);
        }

        public void Dispose() { _disposed = true; Unsubscribe(); }
    }
}
