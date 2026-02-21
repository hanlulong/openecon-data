/**
 * Tests for ProcessingSteps component.
 *
 * Tests step rendering, status display, and duration formatting.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { ProcessingSteps, ProcessingTimelineStep } from '../ProcessingSteps';

describe('ProcessingSteps', () => {
  const mockSteps: ProcessingTimelineStep[] = [
    { step: 'parse', description: 'Parsing query', status: 'completed', durationMs: 150 },
    { step: 'search', description: 'Searching metadata', status: 'completed', durationMs: 200 },
    { step: 'fetch', description: 'Fetching data', status: 'in-progress' },
    { step: 'normalize', description: 'Normalizing data', status: 'pending' },
  ];

  describe('rendering', () => {
    it('renders null when steps array is empty', () => {
      const { container } = render(<ProcessingSteps steps={[]} />);
      expect(container.firstChild).toBeNull();
    });

    it('renders null when steps is undefined', () => {
      const { container } = render(<ProcessingSteps steps={undefined as any} />);
      expect(container.firstChild).toBeNull();
    });

    it('renders all step descriptions', () => {
      render(<ProcessingSteps steps={mockSteps} />);

      expect(screen.getByText('Parsing query')).toBeInTheDocument();
      expect(screen.getByText('Searching metadata')).toBeInTheDocument();
      expect(screen.getByText('Fetching data')).toBeInTheDocument();
      expect(screen.getByText('Normalizing data')).toBeInTheDocument();
    });

    it('applies correct status classes', () => {
      render(<ProcessingSteps steps={mockSteps} />);

      const stepElements = document.querySelectorAll('.processing-step');

      expect(stepElements[0].classList.contains('processing-step--completed')).toBe(true);
      expect(stepElements[1].classList.contains('processing-step--completed')).toBe(true);
      expect(stepElements[2].classList.contains('processing-step--in-progress')).toBe(true);
      expect(stepElements[3].classList.contains('processing-step--pending')).toBe(true);
    });

    it('applies pending modifier class when isPending is true', () => {
      render(<ProcessingSteps steps={mockSteps} isPending />);

      const container = document.querySelector('.processing-steps');
      expect(container?.classList.contains('processing-steps--pending')).toBe(true);
    });

    it('does not apply pending modifier when isPending is false', () => {
      render(<ProcessingSteps steps={mockSteps} isPending={false} />);

      const container = document.querySelector('.processing-steps');
      expect(container?.classList.contains('processing-steps--pending')).toBe(false);
    });
  });

  describe('duration formatting', () => {
    it('displays duration in milliseconds for durations under 1 second', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'quick', description: 'Quick step', status: 'completed', durationMs: 150 },
      ];

      render(<ProcessingSteps steps={steps} />);
      expect(screen.getByText('150ms')).toBeInTheDocument();
    });

    it('displays duration in seconds with one decimal for durations under 10 seconds', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'medium', description: 'Medium step', status: 'completed', durationMs: 2500 },
      ];

      render(<ProcessingSteps steps={steps} />);
      expect(screen.getByText('2.5s')).toBeInTheDocument();
    });

    it('displays duration in rounded seconds for durations 10-60 seconds', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'slow', description: 'Slow step', status: 'completed', durationMs: 25000 },
      ];

      render(<ProcessingSteps steps={steps} />);
      expect(screen.getByText('25s')).toBeInTheDocument();
    });

    it('displays duration in minutes for durations over 60 seconds', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'veryslow', description: 'Very slow step', status: 'completed', durationMs: 90000 },
      ];

      render(<ProcessingSteps steps={steps} />);
      expect(screen.getByText('1.5m')).toBeInTheDocument();
    });

    it('does not display duration when durationMs is undefined', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'pending', description: 'Pending step', status: 'pending' },
      ];

      render(<ProcessingSteps steps={steps} />);
      expect(document.querySelector('.processing-step-duration')).toBeNull();
    });

    it('hides duration when isPending is true', () => {
      const steps: ProcessingTimelineStep[] = [
        { step: 'complete', description: 'Complete step', status: 'completed', durationMs: 100 },
      ];

      render(<ProcessingSteps steps={steps} isPending />);
      expect(document.querySelector('.processing-step-duration')).toBeNull();
    });
  });

  describe('connectors', () => {
    it('renders connectors between steps except for the last one', () => {
      render(<ProcessingSteps steps={mockSteps} />);

      const connectors = document.querySelectorAll('.processing-step-connector');
      expect(connectors.length).toBe(mockSteps.length - 1);
    });

    it('does not render connector for single step', () => {
      const singleStep: ProcessingTimelineStep[] = [
        { step: 'only', description: 'Only step', status: 'completed' },
      ];

      render(<ProcessingSteps steps={singleStep} />);
      expect(document.querySelector('.processing-step-connector')).toBeNull();
    });
  });
});
